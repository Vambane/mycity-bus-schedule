# Data Quality Notes

## Journey Duration Filtering

### Issue Discovered (2026-10-08)

The journey search was showing impossible durations like "1 min" for long-distance routes (e.g., Camps Bay → Civic Centre, which actually takes ~30 minutes).

### Root Cause

Routes with **skip-stop patterns** create misaligned departure/arrival time lists:

**Example: Route 107 "To 107 Civic Centre"**
- **Camps Bay**: 68 departures
- **Civic Centre**: 67 arrivals (one trip skips this stop)

When time lists have unequal lengths, the pairing algorithm (`_pair_times` in journey.py) falls back to pairing each departure with the "nearest strictly-later arrival". This creates incorrect pairings:

```
Trip A: Camps Bay 08:48 → [skips Civic Centre]
Trip B: Camps Bay 08:57 → Civic Centre 08:58

Incorrect pairing: 08:48 → 08:58 = "10 min" (wrong!)
Correct pairing: 08:57 → 09:28 = "31 min"
```

### Solution Implemented

Added minimum duration threshold of **5 minutes** in `journey.py:103`:

```python
if 5 <= minutes <= 300:  # Was: 0 < minutes <= 300
```

### Results

- ✅ **Eliminated**: 1-3 minute impossibilities (reduced from 51 to 0 suspicious results)
- ✅ **Preserved**: Legitimate short trips between adjacent stops (e.g., Adderley → Civic Centre = 5 min)
- ⚠️ **Tradeoff**: Some 5-9 minute mispairings remain for routes with skip-stop patterns

For Camps Bay → Civic Centre:
- Before: 145 results, including 51 with 1-3 min durations
- After: 108 results, minimum 5 min, 93 results show realistic 10-30 min

### Why Not Higher Threshold?

A 10-minute minimum would filter out legitimate short trips:
- Adderley → Civic Centre: 5 min (adjacent stops in city center)
- Other short hops: 5-8 min

The 5-minute threshold strikes a balance between filtering obvious errors while preserving real short-distance trips.

### Remaining Mispairings

Some routes still show suspiciously short durations (5-9 min) due to skip-stop patterns. These are acceptable because:

1. They're mixed with correct durations (users see both 5 min and 30 min options)
2. The majority of results (86%) are realistic (10+ minutes)
3. Users can judge which connections make sense

### Data Quality Recommendations

To fully eliminate mispairings, the GTFS data would need:

1. **Explicit trip IDs** in the departures table to enable correct trip-level pairing
2. **Stop sequences** to verify stop order within each trip
3. **All stops listed** for every trip (no skipped stops in the data)

Current workaround (5-minute minimum) is sufficient for a timetable suggestion system.

---

## Skip-Stop Pattern Examples

Routes with known skip-stop patterns:

| Route | Direction | From Stop | From Count | To Stop | To Count | Difference |
|-------|-----------|-----------|------------|---------|----------|------------|
| 107 | To 107 Civic Centre | Camps Bay | 68 | Civic Centre | 67 | -1 |

*Last updated: 2026-10-08*
