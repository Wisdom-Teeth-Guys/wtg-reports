# HubSpot Workflow: Move Pipedrive Pipelines → Clean Pipelines

**Goal:** Move all open + closed deals from the 6 `*-Wisdom Teeth Guys Pipedrive` pipelines into their corresponding `*-Wisdom Teeth Guys` pipelines (without the Pipedrive suffix). This consolidates duplicate pipelines so existing automation (which targets the clean names) continues to work without reconfiguration.

---

## Pipeline pairs (source → destination)

| City | Source (Pipedrive) Pipeline ID | Destination (Clean) Pipeline ID |
|---|---|---|
| Austin | `2161131227` | `2011021032` |
| Dallas | `2161217265` | `2010971877` |
| Houston | `2161131224` | `2010983139` |
| Phoenix | `2161131216` | `2011043551` |
| San Antonio | `2161131215` | `2011005657` |
| Utah | `2161217271` | `1981854455` |

---

## Build one workflow per city pair

For each city, create one HubSpot Workflow:

### Setup
1. **HubSpot → Automation → Workflows → Create workflow**
2. Type: **Deal-based**
3. **Enrollment trigger** → "Deal properties → Pipeline → is any of → `<Source pipeline for this city>`"
4. **Re-enrollment**: enable, with the same trigger criterion
5. Click **Save** to advance into the action builder

### Action: "If/then branch" on Deal stage

Click **+ → If/then branch**. Add one branch per source stage from the table below, set the **filter** to `Deal stage is exactly <Source stage>`, and inside the branch add a single action:

- **Action**: "Change deal stage"
- **New stage**: pick the destination stage from the table below

This single action sets both pipeline AND stage in one step (HubSpot couples them).

---

## Stage mappings — all 6 cities

> Every Pipedrive stage maps cleanly to a destination stage. No data loss. "Won" → "Closed" and "Lost" → "Closed Lost" since the clean pipelines don't have separate Won/Lost stages.

### Austin (16 stages)
| Source stage (Pipedrive) → | Destination stage (clean) |
|---|---|
| Holding Stage | holding stage |
| Warm Leads | Warm leads |
| Hot Leads | hot leads |
| Wait-Insurance Info | wait-Insurance info |
| Verify Insurance | Verify Insurance |
| Call With Copay | Call with Copay |
| Verified/Not Scheduled | Verified / Not Scheduled |
| Consult/PreAuthorization | Consult / Pre Authorization |
| Scheduled | Scheduled |
| Scheduled For This Week | Scheduled for this Week |
| File Ins Claim | File Ins Claim |
| Follow Up Care | Follow UP Care |
| Claim Follow Up | Claim Follow Up |
| Closed | Closed |
| Won | Closed |
| Lost | Closed Lost |

### Dallas (16 stages)
Same mapping pattern as Austin.

### Houston (14 stages)
| Source stage → | Destination stage |
|---|---|
| Holding Stage - Follow Up | holding stage |
| Lead | Warm leads |
| Wait - Insurance Info | wait-Insurance info |
| Verify Insurance | Verify Insurance |
| Call with Copay | Call with Copay |
| Verified/Not Scheduled | Verified / Not Scheduled |
| Consult/PreAuthorization | Consult / Pre Authorization |
| Scheduled | Scheduled |
| File Insurance Claim | File Ins Claim |
| Follow Up Care - Referral | Follow UP Care |
| Claim Follow Up | Claim Follow Up |
| Closed | Closed |
| Won | Closed |
| Lost | Closed Lost |

### Phoenix (14 stages)
Same pattern as Houston.

### San Antonio (14 stages)
Same pattern as Houston.

### Utah (16 stages)
| Source stage → | Destination stage |
|---|---|
| Holding Stage Follow up | holding stage |
| Warm Leads | Warm leads |
| Hot Leads | hot leads |
| Wait-Insurance Info | wait-Insurance info |
| Verify Insurance | Verify Insurance |
| Call With Copay | Call with Copay |
| Verified/Not Scheduled | Verified / Not Scheduled |
| Consult/PreAuthorization | Consult / Pre Authorization |
| Scheduled | Scheduled |
| Scheduled for Next Date | Scheduled for this Week |
| File Ins Claim | File Ins Claim |
| Follow Up Care - Referral | Follow UP Care |
| Claim Follow Up | Claim Follow Up |
| Closed | Closed |
| Won | Closed |
| Lost | Closed Lost |

---

## Recommended rollout order

1. **Start with one city as a test** (suggest Austin — smallest active volume)
2. Build the workflow, leave it OFF, click **Test** on a single deal to confirm it lands in the right stage of the clean pipeline
3. Turn ON; let it run for 1 day; spot-check a sample of moved deals
4. Repeat for the other 5 cities
5. After all 6 workflows have run and all Pipedrive pipelines are empty, **archive (don't delete)** the Pipedrive pipelines

---

## Full machine-readable mapping

The complete mapping with all stage IDs (so you don't have to look them up in HubSpot) is saved in:

**`PIPELINE_CONSOLIDATION_MAPPING.json`** in this repo.

That JSON has the exact stage IDs you'd need if you ever want to do this via the HubSpot API instead of clicking workflows.

---

## Alternative: one-shot API migration

If you'd rather move all the deals at once (instead of building 6 workflows in HubSpot UI), I can write a Python script that uses the HubSpot Batch Update API to move every Pipedrive deal in a single run (~5 minutes for thousands of deals). Tradeoff:

| | HubSpot Workflows | One-shot API script |
|---|---|---|
| Setup time | ~30 min × 6 = 3 hrs | ~10 min |
| Handles future deals automatically | ✅ Yes (re-enrolls) | ❌ No (one-time only) |
| Visibility / audit | ✅ Visual, HubSpot-native | Logs in GitHub Actions |
| Rollback if wrong | Easy (turn workflow off) | Need backup of old pipeline IDs |

**Recommendation:** if Pipedrive is fully off after June 1, no new deals will land in those Pipedrive pipelines — so the one-shot script is enough. If deals are still being created there, build the workflows for ongoing handling.
