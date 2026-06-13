#!/usr/bin/env python3
"""
Debug script: fetch and print raw XML from ECB SDW to understand structure.
"""

import requests
import xml.etree.ElementTree as ET

series_id = "EXR/M.USD.EUR.SP00.A"  # EUR/USD spot
url = f"https://data-api.ecb.europa.eu/service/data/{series_id}"
headers = {
    "Accept": "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
}

print(f"Fetching: {url}")
response = requests.get(url, headers=headers, verify=False, timeout=10)
print(f"Status: {response.status_code}")
print(f"Content length: {len(response.content)}")
print("\n=== RAW XML (first 3000 chars) ===")
print(response.text[:3000])

# Try to parse
root = ET.fromstring(response.content)
print("\n=== Root tag ===")
print(f"Tag: {root.tag}")
print(f"Attribs: {root.attrib}")

# Print all child tags
print("\n=== Child elements ===")
for i, child in enumerate(root):
    if i < 10:  # First 10
        print(f"  {i}: {child.tag} - attribs: {child.attrib}")
    else:
        print(f"  ... ({len(list(root)) - 10} more)")
        break
