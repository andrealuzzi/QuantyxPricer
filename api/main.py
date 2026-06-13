from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from pathlib import Path
from types import SimpleNamespace
import json
import time
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
from dateutil import parser as date_parser
from typing import Any
import subprocess, sys
import threading
import uuid
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse

app = FastAPI(
    title="Quantyx Pricer API",
    description="API for uploading assets and pricing single or all instruments.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "General", "description": "General API endpoints."},
        {"name": "Assets", "description": "Upload and manage instrument JSON assets."},
        {"name": "Pricing", "description": "Run pricing workflows and read results."},
        {"name": "Jobs", "description": "Track asynchronous pricing jobs."},
    ],
)

# Allow the frontend dev server (vite) and other local tools to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Avoid importing `pricer` at module import time because it imports heavy
# dependencies (QuantLib) that may not be available in the environment.
# Compute the project root and assets path locally so the server can start
# and still support saving asset JSONs without QuantLib installed.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR: Path = PROJECT_ROOT / 'assets'
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
TERMSHEETS_DIR: Path = PROJECT_ROOT / 'termsheets'
TERMSHEETS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR: Path = PROJECT_ROOT / 'output'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Simple in-memory job registry for background tasks (non-persistent)
JOBS = {}
JOBS_LOCK = threading.Lock()


def _write_log(log_path, obj):
    try:
        with open(log_path, 'a') as lf:
            lf.write(json.dumps(obj) + '\n')
    except Exception:
        pass


def _run_price_all(job_id: str, cmd: list, log_path: Path):
    with JOBS_LOCK:
        JOBS[job_id]['status'] = 'running'
        JOBS[job_id]['start_ts'] = datetime.utcnow().isoformat() + 'Z'
    _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'price_all', 'job': job_id, 'phase': 'started', 'cmd': ' '.join(cmd) })
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
        # write stdout/stderr to job
        with JOBS_LOCK:
            JOBS[job_id]['stdout'] = stdout[:500000]
            JOBS[job_id]['stderr'] = stderr[:500000]
            JOBS[job_id]['returncode'] = proc.returncode
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'failed'
            JOBS[job_id]['end_ts'] = datetime.utcnow().isoformat() + 'Z'
            JOBS[job_id]['error'] = str(e)
        _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'price_all', 'job': job_id, 'phase': 'failed', 'error': str(e) })
        return

    # after running, try to read output/prices.json
    out_path = PROJECT_ROOT / 'output' / 'prices.json'
    if out_path.exists() and (JOBS.get(job_id) is not None):
        try:
            with open(out_path, 'r') as f:
                data = json.load(f)
            with JOBS_LOCK:
                JOBS[job_id]['status'] = 'succeeded'
                JOBS[job_id]['end_ts'] = datetime.utcnow().isoformat() + 'Z'
                JOBS[job_id]['result_count'] = len(data) if isinstance(data, list) else None
            _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'price_all', 'job': job_id, 'phase': 'succeeded', 'stdout': stdout[:2000] })
        except Exception as e:
            with JOBS_LOCK:
                JOBS[job_id]['status'] = 'failed'
                JOBS[job_id]['end_ts'] = datetime.utcnow().isoformat() + 'Z'
                JOBS[job_id]['error'] = f'Could not read prices.json: {e}'
            _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'price_all', 'job': job_id, 'phase': 'read_failed', 'error': str(e) })
    else:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'failed'
            JOBS[job_id]['end_ts'] = datetime.utcnow().isoformat() + 'Z'
            JOBS[job_id]['error'] = 'prices.json not produced'
        _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'price_all', 'job': job_id, 'phase': 'no_output', 'stdout': stdout[:2000] if 'stdout' in locals() else '' })


@app.get('/', include_in_schema=False)
async def root():
    return RedirectResponse(url='/docs')



@app.post('/assets', tags=['Assets'], summary='Upload an asset JSON file')
async def save_asset(request: Request, payload: dict = None):
    """Save a bond JSON into the assets/ folder.

    Body must be the bond JSON itself (object) and include `instrument_id` or `isin`.
    Returns the saved filename.
    """
    # Log incoming request for debugging
    try:
        raw_body = await request.body()
        print('\n[API] /assets received request headers:')
        for k, v in request.headers.items():
            print(f'  {k}: {v}')
        print(f'[API] Raw body length: {len(raw_body)}')
    except Exception as e:
        print(f'[API] Could not read raw request body: {e}')

    if payload is None:
        # Attempt to parse JSON from raw body for more helpful error messages
        try:
            payload = json.loads(raw_body.decode('utf-8')) if raw_body else None
        except Exception as e:
            raise HTTPException(status_code=400, detail=f'Invalid JSON body: {e}')

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='JSON object required in request body')
    bond = payload

    # Normalize common date fields into ISO YYYY-MM-DD
    def try_parse_date(val: Any):
        if not isinstance(val, str):
            return None
        s = val.strip()
        if not s:
            return None
        # Fast reject values that look like already ISO
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            return s[:10]
        try:
            dt = date_parser.parse(s, dayfirst=True)
            return dt.date().isoformat()
        except Exception:
            return None

    date_keys = [
        'evaluation_date', 'maturity_date', 'first_coupon_date', 'issue_date',
        'interest_commencement_date', 'expiry_date', 'first_day_of_trading'
    ]
    for k in date_keys:
        if k in bond:
            parsed = try_parse_date(bond[k])
            if parsed:
                bond[k] = parsed
            else:
                print(f"[API] Could not parse date field {k}: {bond.get(k)}")
    instrument_id = bond.get('instrument_id') or bond.get('isin')
    if not instrument_id:
        raise HTTPException(status_code=400, detail='bond JSON must include instrument_id or isin')
    filename = f"{instrument_id}.json"
    path = ASSETS_DIR / filename
    try:
        with open(path, 'w') as f:
            json.dump(bond, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    print(f"[API] Saved asset to {path} (size={path.stat().st_size} bytes)")
    return {"saved": filename, "path": str(path)}


@app.post('/update_asset', tags=['Assets'], summary='Replace an existing asset JSON by uploaded filename')
async def update_asset(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail='Uploaded file must include a filename')

    filename = Path(file.filename).name
    if not filename.lower().endswith('.json'):
        raise HTTPException(status_code=400, detail='Only .json files are supported')

    path = ASSETS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f'Asset file not found: {filename}')

    try:
        raw = await file.read()
        payload = json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Invalid JSON file: {e}')

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='JSON root must be an object')

    # Keep date normalization consistent with /assets endpoint behavior.
    def try_parse_date(val: Any):
        if not isinstance(val, str):
            return None
        s = val.strip()
        if not s:
            return None
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            return s[:10]
        try:
            dt = date_parser.parse(s, dayfirst=True)
            return dt.date().isoformat()
        except Exception:
            return None

    date_keys = [
        'evaluation_date', 'maturity_date', 'first_coupon_date', 'issue_date',
        'interest_commencement_date', 'expiry_date', 'first_day_of_trading'
    ]
    for k in date_keys:
        if k in payload:
            parsed = try_parse_date(payload[k])
            if parsed:
                payload[k] = parsed

    try:
        with open(path, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not update asset file: {e}')

    return {"updated": filename, "path": str(path)}


@app.post('/termsheet_asset', tags=['Assets'], summary='Upload a PDF termsheet and convert it to an asset JSON')
async def termsheet_asset(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail='Uploaded file must include a filename')

    filename = Path(file.filename).name
    if not filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='Only .pdf files are supported for termsheet upload')

    temp_pdf = TERMSHEETS_DIR / f"{uuid.uuid4().hex}_{filename}"
    try:
        raw = await file.read()
        with open(temp_pdf, 'wb') as f:
            f.write(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not store uploaded termsheet: {e}')

    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from scripts import termsheet_to_json as ts2j
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not load termsheet converter: {e}')

    try:
        # Let the converter derive output filename from detected ISIN (or PDF stem).
        ts2j.process_file(temp_pdf, ASSETS_DIR)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Termsheet conversion failed: {e}')

    # Try to discover generated JSON name from parsed ISIN, with fallback to PDF stem.
    try:
        text = ts2j.extract_text_from_pdf(temp_pdf)
        guessed = ts2j.heuristic_field_from_text(text)
        instrument_id = guessed.get('instrument_id') or temp_pdf.stem.split('_', 1)[-1]
    except Exception:
        instrument_id = temp_pdf.stem.split('_', 1)[-1]

    out_file = f"{instrument_id}.json"
    out_path = ASSETS_DIR / out_file
    if not out_path.exists():
        # fallback: converter may have used source stem
        fallback = ASSETS_DIR / f"{Path(filename).stem}.json"
        if fallback.exists():
            out_path = fallback
            out_file = fallback.name
        else:
            raise HTTPException(status_code=500, detail='Conversion finished but output JSON was not found')

    try:
        with open(out_path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not read generated JSON: {e}')

    return {
        'saved': out_file,
        'path': str(out_path),
        'instrument_id': payload.get('instrument_id') or payload.get('isin'),
        'asset': payload,
    }


@app.get('/fetch_asset', tags=['Assets'], summary='Fetch one asset JSON by instrument_id')
async def fetch_asset(instrument_id: str):
    if not instrument_id or not instrument_id.strip():
        raise HTTPException(status_code=400, detail='instrument_id is required')

    # Keep only the basename and map to assets/<instrument_id>.json
    safe_id = Path(instrument_id.strip()).name
    asset_path = ASSETS_DIR / f"{safe_id}.json"
    if not asset_path.exists():
        raise HTTPException(status_code=404, detail=f'Asset not found for instrument_id: {safe_id}')

    try:
        with open(asset_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not read asset file: {e}')


@app.get('/fetch_termsheet', tags=['Assets'], summary='Fetch one termsheet PDF by instrument_id')
async def fetch_termsheet(instrument_id: str):
    if not instrument_id or not instrument_id.strip():
        raise HTTPException(status_code=400, detail='instrument_id is required')

    safe_id = Path(instrument_id.strip()).name
    pdf_path = TERMSHEETS_DIR / f"{safe_id}.pdf"
    if not pdf_path.exists():
        # fallback: try case-insensitive lookup
        candidates = [p for p in TERMSHEETS_DIR.glob('*.pdf') if p.stem.lower() == safe_id.lower()]
        if candidates:
            pdf_path = candidates[0]
        else:
            raise HTTPException(status_code=404, detail=f'Termsheet not found for instrument_id: {safe_id}')

    return FileResponse(
        path=str(pdf_path),
        media_type='application/pdf',
        headers={"Content-Disposition": f'inline; filename="{pdf_path.name}"'},
    )


@app.get('/fetch_report', tags=['Assets'], summary='Fetch one output report PDF by instrument_id')
async def fetch_report(instrument_id: str):
    if not instrument_id or not instrument_id.strip():
        raise HTTPException(status_code=400, detail='instrument_id is required')

    safe_id = Path(instrument_id.strip()).name
    pdf_path = OUTPUT_DIR / f"{safe_id}.pdf"
    if not pdf_path.exists():
        # fallback: try case-insensitive lookup
        candidates = [p for p in OUTPUT_DIR.glob('*.pdf') if p.stem.lower() == safe_id.lower()]
        if candidates:
            pdf_path = candidates[0]
        else:
            raise HTTPException(status_code=404, detail=f'Report not found for instrument_id: {safe_id}')

    return FileResponse(
        path=str(pdf_path),
        media_type='application/pdf',
        headers={"Content-Disposition": f'inline; filename="{pdf_path.name}"'},
    )


@app.post('/price', tags=['Pricing'], summary='Price one instrument by InstrumentId')
async def price(request: Request, payload: dict = None):
    """Price a single bond.

    Accepts either:
    - { "bond_file": "FR0013398757.json" }  # filename under assets/ or absolute path
    - { "bond": { ... } }                    # full bond JSON (will be saved to assets/ then priced)

    Returns the pricer result JSON (same format as entries in prices.json).
    """
    # Read raw body and log request metadata
    raw_body = None
    try:
        raw_body = await request.body()
    except Exception:
        raw_body = None

    # Prepare log entry
    log_path = PROJECT_ROOT / 'output' / 'price_requests.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        'ts': datetime.utcnow().isoformat() + 'Z',
        'client': None,
        'instrument': None,
        'status': None,
        'msg': None,
    }
    try:
        entry['client'] = request.client.host if request.client else None
    except Exception:
        entry['client'] = None

    # Parse JSON body if not provided
    if payload is None:
        try:
            payload = json.loads(raw_body.decode('utf-8')) if raw_body else None
        except Exception:
            payload = None

    try:
        if isinstance(payload, dict):
            entry['instrument'] = payload.get('InstrumentId') or payload.get('instrument_id')
    except Exception:
        pass

    # write initial log line (incoming request)
    try:
        with open(log_path, 'a') as lf:
            lf.write(json.dumps({**entry, 'event': 'incoming'}) + '\n')
    except Exception:
        pass

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='JSON body required')

    # If InstrumentId provided, call pricer CLI and return the pricer result
    instr = payload.get('InstrumentId') if isinstance(payload, dict) else None
    if instr:
        entry['instrument'] = instr
        try:
            with open(log_path, 'a') as lf:
                lf.write(json.dumps({**entry, 'event': 'pricing_started'}) + '\n')
        except Exception:
            pass
        # call pricer as a CLI: python pricer.py --bond <InstrumentId>
        pricer_py = PROJECT_ROOT / 'pricer.py'
        cmd = [sys.executable, str(pricer_py), '--bond', str(instr)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as e:
            entry['status'] = 500
            entry['msg'] = f'Failed to run pricer CLI: {e}'
            try:
                with open(log_path, 'a') as lf:
                    lf.write(json.dumps({**entry, 'event': 'pricing_failed'}) + '\n')
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f'Failed to run pricer CLI: {e}')

        # Log CLI output for debugging
        print(f"[API] pricer CLI stdout:\n{proc.stdout}")
        if proc.stderr:
            print(f"[API] pricer CLI stderr:\n{proc.stderr}")

        # Try to import pricer module and return structured result via dispatch_one
        # Ensure project root is on sys.path so the parent-level pricer.py is importable
        try:
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            import pricer
        except Exception as e:
            entry['status'] = 500
            entry['msg'] = f'Could not import pricer module after running CLI: {e}'
            try:
                with open(log_path, 'a') as lf:
                    lf.write(json.dumps({**entry, 'event': 'pricing_failed'}) + '\n')
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f'Could not import pricer module after running CLI: {e}')

        curve_path = pricer.resolve_curve_path(str(pricer.DEFAULT_CURVE_FILE))
        try:
            curve_json = pricer.hullwhite.load_json(curve_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f'Could not load curve file: {e}')

        # resolve asset path for the provided instrument id
        bond_file = pricer.resolve_asset_path(str(instr))
        try:
            result = pricer.dispatch_one(Path(bond_file), curve_json, SimpleNamespace(
                issuer_spread_bp=None,
                tree_steps=None,
                time_steps=None,
                num_paths=None,
                seed=None,
                bond=None,
                bond_file=None,
                curve_file=str(curve_path),
            ))
            entry['status'] = 200
            entry['msg'] = 'pricing_succeeded'
            try:
                with open(log_path, 'a') as lf:
                    lf.write(json.dumps({**entry, 'event': 'pricing_succeeded'}) + '\n')
            except Exception:
                pass
            return result
        except Exception as e:
            entry['status'] = 500
            entry['msg'] = f'Could not price instrument: {e}'
            try:
                with open(log_path, 'a') as lf:
                    lf.write(json.dumps({**entry, 'event': 'pricing_failed', 'error': str(e)}) + '\n')
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f'Could not price instrument {instr}: {e}')

    # Deprecated: other modes removed. Ask client to send InstrumentId.
    raise HTTPException(status_code=400, detail='Provide "InstrumentId" in request body')


@app.get('/jobs/{job_id}', tags=['Jobs'], summary='Get async job status')
async def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    # return a safe subset
    safe = {k: job.get(k) for k in ['id', 'status', 'cmd', 'created_ts', 'start_ts', 'end_ts', 'returncode', 'error', 'result_count']}
    return safe


@app.get('/prices', tags=['Pricing'], summary='Get latest generated prices.json')
async def get_prices(request: Request):
    """Return the generated output/prices.json if present and log access attempts."""
    out_path = PROJECT_ROOT / 'output' / 'prices.json'
    log_path = PROJECT_ROOT / 'output' / 'prices_access.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        'ts': datetime.utcnow().isoformat() + 'Z',
        'client': None,
        'path': str(request.url.path),
        'status': None,
        'msg': None,
    }
    try:
        entry['client'] = request.client.host if request.client else None
    except Exception:
        entry['client'] = None

    if not out_path.exists():
        entry['status'] = 404
        entry['msg'] = 'prices.json not found'
        try:
            with open(log_path, 'a') as lf:
                lf.write(json.dumps(entry) + '\n')
        except Exception:
            pass
        print(f"[API] /prices - 404 - {entry['msg']} - client={entry['client']}")
        raise HTTPException(status_code=404, detail='prices.json not found')

    try:
        with open(out_path, 'r') as f:
            data = json.load(f)
        entry['status'] = 200
        try:
            entry['msg'] = f"served {len(data)} entries" if isinstance(data, list) else 'served object'
        except Exception:
            entry['msg'] = 'served data'
        try:
            with open(log_path, 'a') as lf:
                lf.write(json.dumps(entry) + '\n')
        except Exception:
            pass
        print(f"[API] /prices - 200 OK - served to {entry['client']}")
        return data
    except Exception as e:
        entry['status'] = 500
        entry['msg'] = f'Could not read prices.json: {e}'
        try:
            with open(log_path, 'a') as lf:
                lf.write(json.dumps(entry) + '\n')
        except Exception:
            pass
        print(f"[API] /prices - 500 - {e}")
        raise HTTPException(status_code=500, detail=entry['msg'])


@app.get('/fetch_noprice_assets', tags=['Assets'], summary='List asset instrument_ids not present in prices.json')
async def fetch_noprice_assets():
    out_path = PROJECT_ROOT / 'output' / 'prices.json'
    if not out_path.exists():
        raise HTTPException(status_code=404, detail='prices.json not found')

    try:
        with open(out_path, 'r') as f:
            price_rows = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not read prices.json: {e}')

    priced_ids = set()
    if isinstance(price_rows, list):
        for row in price_rows:
            if not isinstance(row, dict):
                continue
            instrument_id = row.get('instrument_id') or (row.get('result') or {}).get('instrument_id')
            bond_file = row.get('bond_file')
            if instrument_id:
                priced_ids.add(str(instrument_id))
            if bond_file:
                priced_ids.add(Path(str(bond_file)).stem)

    missing_ids = []
    for asset_path in sorted(ASSETS_DIR.glob('*.json')):
        asset_id = asset_path.stem
        if asset_id not in priced_ids:
            missing_ids.append(asset_id)

    return {
        'missing_instrument_ids': missing_ids,
        'count': len(missing_ids),
    }


@app.post('/price_all', tags=['Pricing'], summary='Start async pricing for all instruments')
async def price_all(request: Request):
    """Trigger pricing for all bonds by running `pricer.py --bond all` and return the generated prices.json."""
    log_path = PROJECT_ROOT / 'output' / 'price_requests.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        'ts': datetime.utcnow().isoformat() + 'Z',
        'client': None,
        'status': None,
        'msg': None,
        'event': 'price_all'
    }
    try:
        entry['client'] = request.client.host if request.client else None
    except Exception:
        entry['client'] = None

    # log incoming
    try:
        with open(log_path, 'a') as lf:
            lf.write(json.dumps({**entry, 'phase': 'incoming'}) + '\n')
    except Exception:
        pass

    # Run pricer CLI in background thread and return a job id for polling
    pricer_py = PROJECT_ROOT / 'pricer.py'
    cmd = [sys.executable, str(pricer_py), '--bond', 'all']

    job_id = uuid.uuid4().hex
    job = {
        'id': job_id,
        'status': 'pending',
        'cmd': ' '.join(cmd),
        'created_ts': datetime.utcnow().isoformat() + 'Z',
        'start_ts': None,
        'end_ts': None,
        'stdout': None,
        'stderr': None,
        'returncode': None,
        'error': None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    try:
        _write_log(log_path, {**entry, 'phase': 'enqueued', 'job': job_id, 'cmd': job['cmd'] })
    except Exception:
        pass

    t = threading.Thread(target=_run_price_all, args=(job_id, cmd, log_path), daemon=True)
    t.start()

    return JSONResponse(status_code=202, content={'job_id': job_id, 'status_url': f'/jobs/{job_id}'})


def _run_update_curve(job_id: str, cmd: list, log_path: Path):
    with JOBS_LOCK:
        JOBS[job_id]['status'] = 'running'
        JOBS[job_id]['start_ts'] = datetime.utcnow().isoformat() + 'Z'
    _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'update_curve', 'job': job_id, 'phase': 'started', 'cmd': ' '.join(cmd) })
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
        with JOBS_LOCK:
            JOBS[job_id]['stdout'] = stdout[:500000]
            JOBS[job_id]['stderr'] = stderr[:500000]
            JOBS[job_id]['returncode'] = proc.returncode
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'failed'
            JOBS[job_id]['end_ts'] = datetime.utcnow().isoformat() + 'Z'
            JOBS[job_id]['error'] = str(e)
        _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'update_curve', 'job': job_id, 'phase': 'failed', 'error': str(e) })
        return

    with JOBS_LOCK:
        JOBS[job_id]['status'] = 'succeeded'
        JOBS[job_id]['end_ts'] = datetime.utcnow().isoformat() + 'Z'
    _write_log(log_path, { 'ts': datetime.utcnow().isoformat() + 'Z', 'event': 'update_curve', 'job': job_id, 'phase': 'succeeded', 'stdout': stdout[:2000] })


@app.post('/update_curves', tags=['General'], summary='Start async swap curve update (ECB)')
async def update_curves(request: Request, payload: dict = None):
    """Trigger swap curve update by running `scripts/update_curve.py` in background and return a job id."""
    log_path = PROJECT_ROOT / 'output' / 'update_curves.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # read optional body
    raw_body = None
    try:
        raw_body = await request.body()
    except Exception:
        raw_body = None
    if payload is None and raw_body:
        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except Exception:
            payload = None

    curve_file = None
    try:
        if isinstance(payload, dict):
            curve_file = payload.get('curve_file') or payload.get('curve')
    except Exception:
        curve_file = None

    # also allow query param ?curve_file=...
    try:
        q = dict(request.query_params)
        if 'curve_file' in q and q.get('curve_file'):
            curve_file = q.get('curve_file')
    except Exception:
        pass

    script_path = PROJECT_ROOT / 'scripts' / 'update_curves.py'
    cmd = [sys.executable, str(script_path)]
    if curve_file:
        cmd += ['--curve-file', str(curve_file)]

    job_id = uuid.uuid4().hex
    job = {
        'id': job_id,
        'status': 'pending',
        'cmd': ' '.join(cmd),
        'created_ts': datetime.utcnow().isoformat() + 'Z',
        'start_ts': None,
        'end_ts': None,
        'stdout': None,
        'stderr': None,
        'returncode': None,
        'error': None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    try:
        _write_log(log_path, {'phase': 'enqueued', 'job': job_id, 'cmd': job['cmd'], 'ts': datetime.utcnow().isoformat() + 'Z'})
    except Exception:
        pass

    t = threading.Thread(target=_run_update_curve, args=(job_id, cmd, log_path), daemon=True)
    t.start()

    return JSONResponse(status_code=202, content={'job_id': job_id, 'status_url': f'/jobs/{job_id}'})
