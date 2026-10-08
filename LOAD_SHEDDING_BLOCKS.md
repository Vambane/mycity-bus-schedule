# Load Shedding Block Configuration

## Current Status

✅ **Load shedding integration is working** but in **degraded mode**:
- Stage detection works (ESP API)
- Stage indicator shows in header
- **Block-based disruption warnings are disabled** (missing `ls_block` column)

## Why Blocks Matter

Cape Town uses a 16-block rotational load shedding schedule. Each stop belongs to a specific block (1-16), which determines when it experiences outages. With block data:
- ✅ Show which connections are affected by current outages
- ✅ Display duration buffers (+10 min for affected routes)
- ✅ Warn users about stop-specific outage windows
- ✅ Help riders plan around load shedding

## Adding Block Data

### Option 1: Manual Addition (Quick)

Add an `ls_block` column to your stops table:

```sql
-- Connect to database
duckdb data/myciti.duckdb

-- Add column
ALTER TABLE stops ADD COLUMN ls_block INTEGER;

-- Update blocks for known stops (example)
UPDATE stops SET ls_block = 7 WHERE stop_name = 'Civic Centre';
UPDATE stops SET ls_block = 3 WHERE stop_name = 'Camps Bay';
UPDATE stops SET ls_block = 5 WHERE stop_name = 'Sea Point';
-- ... etc for all stops

-- Verify
SELECT stop_name, ls_block FROM stops WHERE ls_block IS NOT NULL LIMIT 10;
```

### Option 2: CSV Import (Recommended)

1. **Create a CSV file** (`data/stop_blocks.csv`):
```csv
stop_name,ls_block
Civic Centre,7
Camps Bay,3
Sea Point,5
Hout Bay,2
...
```

2. **Import into database**:
```sql
-- Connect
duckdb data/myciti.duckdb

-- Add column if not exists
ALTER TABLE stops ADD COLUMN ls_block INTEGER;

-- Import from CSV
CREATE TEMP TABLE temp_blocks AS
SELECT * FROM read_csv_auto('data/stop_blocks.csv');

-- Update stops table
UPDATE stops
SET ls_block = temp_blocks.ls_block
FROM temp_blocks
WHERE stops.stop_name = temp_blocks.stop_name;

-- Verify
SELECT COUNT(*) as stops_with_blocks
FROM stops
WHERE ls_block IS NOT NULL;
```

### Option 3: Geocoding (Advanced)

Use City of Cape Town's official block boundaries:

```python
import geopandas as gpd
import duckdb

# Load block boundaries (GeoJSON from CCT)
blocks = gpd.read_file('data/cct_loadshedding_blocks.geojson')

# Load stops
conn = duckdb.connect('data/myciti.duckdb')
stops = conn.execute("""
    SELECT DISTINCT stop_name, stop_lat, stop_lon
    FROM stops
    WHERE stop_lat IS NOT NULL
""").df()

# Spatial join
stops_gdf = gpd.GeoDataFrame(
    stops,
    geometry=gpd.points_from_xy(stops.stop_lon, stops.stop_lat),
    crs='EPSG:4326'
)
joined = gpd.sjoin(stops_gdf, blocks, how='left', predicate='within')

# Update database
conn.execute("ALTER TABLE stops ADD COLUMN ls_block INTEGER")
for _, row in joined.iterrows():
    conn.execute(
        "UPDATE stops SET ls_block = ? WHERE stop_name = ?",
        [row['block_number'], row['stop_name']]
    )
```

## Finding Block Data

### Official Sources

1. **City of Cape Town**
   - Website: https://www.capetown.gov.za/Family%20and%20home/residential-utility-services/residential-electricity-services/load-shedding-and-outages
   - Search for your area's block number

2. **EskomSePush API**
   - Endpoint: `/areas_nearby` (lat/lon → block)
   - Endpoint: `/areas_search` (search by name)

3. **Manual Lookup**
   - Visit: https://loadshedding.eskom.co.za/
   - Enter address to find block

### Sample Data

Here are some known blocks for major MyCiTi stops:

```
Civic Centre: Block 7
Camps Bay: Block 3
Sea Point: Block 5
Hout Bay: Block 2
Milnerton: Block 11
Table View: Block 13
Century City: Block 9
```

## Verifying It Works

After adding block data, restart the app and:

1. **Set a manual stage** (if no ESP key):
   - The stage indicator will show "Stage X (default)"
   - Blocks need stage > 0 to calculate outages

2. **Search for a journey**:
   - If blocks match outage windows, you'll see:
     - ⚡ "Stage-affected" amber badge
     - Duration buffer: "~52 min (+10 buffer)"
     - Warning: "Load shedding at origin (block 7): 14:00 to 16:30"

3. **View a stop**:
   - If stop's block has outages today:
     - Warning banner at top
     - "⚡ Load shedding at {stop} (block {N}) today: ..."

## Current Behavior (Without Blocks)

The app **works perfectly** without block data:
- ✅ Journey search works
- ✅ Stop timetables work
- ✅ System map works
- ✅ Stage indicator shows
- ⚠️ Block-specific warnings don't show

All functionality is **gracefully degraded** - no errors, just fewer warnings.

## When to Add Blocks

**Add blocks if**:
- You have ESP API access
- Users experience frequent load shedding
- Accurate disruption warnings are important

**Skip blocks if**:
- Load shedding is rare
- Users understand general stage info is enough
- You don't have access to block data

---

**Note**: The FastAPI app is **production-ready without blocks**. Adding them is optional but enhances the user experience during load shedding events.
