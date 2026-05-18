#!/usr/bin/env python3
"""
Build a paused HubSpot workflow that syncs zip between a deal and its newly
associated company.

Trigger: list-based on `number_of_associated_companies IS_KNOWN`, with
re-enrollment when that property changes (HubSpot's proven pattern, same as
the existing 'Attach company name from association to deal' workflow).

Action: custom JS that:
  1. Reads deal.postal_code (and falls back to migrated_zip_code / zip_code)
  2. Looks up the associated company's `zip`
  3. Bidirectional fill — whichever side is missing gets the other side's value
  4. Outputs `zip` which the next action writes to deal.postal_code

Created PAUSED so we can test before activating.
"""

import json
import os
import sys
import requests
from pathlib import Path

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN")
if not HUBSPOT_TOKEN:
    print("Set HUBSPOT_TOKEN", file=sys.stderr); sys.exit(1)

BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
NAME = "[PAUSED] Sync Zip on Company Association (Deal ↔ Company)"

SOURCE_CODE = r"""const hubspot = require('@hubspot/api-client');

// LOGIC: Company zip wins when known. If company is missing zip, keep deal as-is
// (never write to the company — avoids polluting insurance-company records, etc.)

exports.main = async (event, callback) => {
  const dealPostal = (event.inputFields['postal_code'] || '').trim();
  const migrated   = (event.inputFields['migrated_zip_code'] || '').trim();
  const zipCode    = (event.inputFields['zip_code'] || '').trim();
  const dealOwnZip = dealPostal || migrated || zipCode || '';
  console.log('[zip-sync] inputs:', { dealPostal, migrated, zipCode, dealOwnZip });

  let resultZip = dealOwnZip;
  let action = 'no-change';
  let debug = '';

  try {
    const client = new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
    const dealId = event.object.objectId;
    console.log('[zip-sync] dealId:', dealId);

    const assocResp = await client.crm.associations.v4.basicApi.getPage(
      'deals', dealId, 'companies'
    );
    const results = assocResp.results || [];
    console.log('[zip-sync] associations found:', results.length);
    debug = `assoc=${results.length}`;

    if (results.length > 0) {
      const companyId = results[0].toObjectId;
      console.log('[zip-sync] first company id:', companyId);
      debug += `;coId=${companyId}`;

      const co = await client.crm.companies.basicApi.getById(companyId, ['zip']);
      const companyZip = (co.properties.zip || '').trim();
      debug += `;coZip="${companyZip}"`;

      if (companyZip) {
        resultZip = companyZip;
        action = 'pull-company-zip-to-deal';
        console.log('[zip-sync] PULL: using company zip:', companyZip);
      } else {
        action = 'skip-company-zip-empty';
        console.log('[zip-sync] SKIP: company zip is empty — leaving everything alone');
      }
    } else {
      action = 'skip-no-company';
      console.log('[zip-sync] SKIP: no company associated');
    }
  } catch (e) {
    console.log('[zip-sync] ERROR:', e.message || e);
    if (e.response && e.response.body) console.log('[zip-sync] error body:', JSON.stringify(e.response.body));
    debug += `;err=${e.message || e}`;
    action = 'error';
  }

  console.log('[zip-sync] action:', action, 'final resultZip:', resultZip);
  callback({ outputFields: { zip: resultZip, debug: debug, action: action } });
};
"""


def build_flow():
    src_filter = lambda prop: {
        "filterBranches": [],
        "filters": [{
            "property": prop,
            "operation": {"operator": "IS_KNOWN", "includeObjectsWithNoValueSet": False, "operationType": "ALL_PROPERTY"},
            "filterType": "PROPERTY",
        }],
        "filterBranchType": "AND", "filterBranchOperator": "AND",
    }
    return {
        "name": NAME,
        "description": (
            "Triggers when a deal gains/changes an associated company. "
            "If the company has a zip, copy it onto deal.postal_code (referral source "
            "drives territory). If the company has no zip, do nothing to the company — "
            "deal.postal_code falls back to the deal's existing zip "
            "(postal_code → migrated_zip_code → zip_code). Never writes to companies, "
            "so insurance-company associations can't get corrupted. Paused for testing."
        ),
        "isEnabled": False,
        "type": "PLATFORM_FLOW",
        "objectTypeId": "0-3",
        "flowType": "WORKFLOW",
        "startActionId": "1",
        "nextAvailableActionId": "3",
        "timeWindows": [],
        "blockedDates": [],
        "customProperties": {},
        "dataSources": [],
        "crmObjectCreationStatus": "ACTIVE",
        "enrollmentCriteria": {
            "shouldReEnroll": True,
            "listFilterBranch": {
                "filterBranches": [src_filter("number_of_associated_companies")],
                "filters": [],
                "filterBranchType": "OR", "filterBranchOperator": "OR",
            },
            "unEnrollObjectsNotMeetingCriteria": False,
            "reEnrollmentTriggersFilterBranches": [{
                "filterBranches": [],
                "filters": [
                    {"property": "hs_name", "operation": {
                        "operator": "IS_EQUAL_TO", "includeObjectsWithNoValueSet": False,
                        "value": "number_of_associated_companies", "operationType": "STRING"}, "filterType": "PROPERTY"},
                    {"property": "hs_value", "operation": {
                        "operator": "IS_KNOWN", "includeObjectsWithNoValueSet": False,
                        "operationType": "ALL_PROPERTY"}, "filterType": "PROPERTY"},
                ],
                "filterBranchType": "AND", "filterBranchOperator": "AND",
            }],
            "type": "LIST_BASED",
        },
        "actions": [
            {
                "actionId": "1",
                "secretNames": [],
                "sourceCode": SOURCE_CODE,
                "runtime": "NODE20X",
                "inputFields": [
                    {"name": "postal_code", "value": {"propertyName": "postal_code", "type": "OBJECT_PROPERTY"}},
                    {"name": "migrated_zip_code", "value": {"propertyName": "migrated_zip_code", "type": "OBJECT_PROPERTY"}},
                    {"name": "zip_code", "value": {"propertyName": "zip_code", "type": "OBJECT_PROPERTY"}},
                ],
                "outputFields": [
                    {"name": "zip", "type": "STRING"},
                    {"name": "debug", "type": "STRING"},
                    {"name": "action", "type": "STRING"},
                ],
                "connection": {"edgeType": "STANDARD", "nextActionId": "2"},
                "type": "CUSTOM_CODE",
            },
            {
                "actionId": "2",
                "actionTypeVersion": 0,
                "actionTypeId": "0-5",
                "fields": {
                    "property_name": "postal_code",
                    "value": {"actionId": "1", "dataKey": "zip", "type": "FIELD_DATA"},
                },
                "type": "SINGLE_CONNECTION",
            },
        ],
    }


def find_existing(name):
    after = None
    while True:
        p = {"limit": 100}
        if after: p["after"] = after
        r = requests.get(f"{BASE}/automation/v4/flows", headers=H, params=p); r.raise_for_status()
        body = r.json()
        for f in body.get("results", []):
            if f.get("name") == name:
                return f["id"]
        pg = body.get("paging", {}).get("next")
        if not pg: return None
        after = pg.get("after")


def main():
    existing = find_existing(NAME)
    flow = build_flow()

    if existing and "--recreate" not in sys.argv:
        # PATCH to update in place — keeps secrets attached to the action
        print(f"Updating existing workflow {existing} in place (secrets preserved)…")
        r = requests.patch(f"{BASE}/automation/v4/flows/{existing}", headers=H, json=flow)
        if r.status_code in (200, 201):
            d = r.json()
            print(f"✓ Updated '{d['name']}' (id={d['id']}, enabled={d['isEnabled']})")
        else:
            print(f"✗ PATCH {r.status_code}: {r.text[:500]}")
        return

    if existing and "--recreate" in sys.argv:
        print(f"Deleting existing {existing} (secrets will need re-adding)…")
        requests.delete(f"{BASE}/automation/v4/flows/{existing}", headers=H)

    r = requests.post(f"{BASE}/automation/v4/flows", headers=H, json=flow)
    if r.status_code in (200, 201):
        d = r.json()
        print(f"✓ Created '{d['name']}' (id={d['id']}, enabled={d['isEnabled']})")
    else:
        print(f"✗ POST {r.status_code}: {r.text[:500]}")


if __name__ == "__main__":
    main()
