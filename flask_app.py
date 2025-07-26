import os
from flask import Flask, render_template, request
import pandas as pd
import requests
import folium
from folium.features import DivIcon
from folium.plugins import AntPath
from google.transit import gtfs_realtime_pb2
from datetime import datetime, timedelta
import pytz
import time

# --- Constants ---
VEHICLE_POSITIONS_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Bus"
TRIP_UPDATES_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Bus"
BRISBANE_TZ = pytz.timezone('Australia/Brisbane')
REFRESH_INTERVAL_SECONDS = 60

# --- Flask App Setup ---
app = Flask(__name__)

# --- Simple In-Memory Cache ---
# In a production environment, you might use Flask-Caching with Redis or Memcached,
# but for PythonAnywhere's basic tier, a simple global dict is fine.
CACHE = {
    "data": pd.DataFrame(),
    "last_refreshed": None,
    "previous_data": pd.DataFrame()
}

# --- Data Fetching & Processing Logic (similar to before) ---

def fetch_gtfs_rt(url: str) -> bytes | None:
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except requests.RequestException as e:
        print(f"Error fetching GTFS-RT data: {e}")
        return None

def parse_vehicle_positions(content: bytes) -> pd.DataFrame:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    vehicles = [
        {
            "trip_id": v.trip.trip_id, "route_id": v.trip.route_id, "vehicle_id": v.vehicle.label,
            "lat": v.position.latitude, "lon": v.position.longitude, "stop_sequence": v.current_stop_sequence,
            "stop_id": v.stop_id, "current_status": v.current_status,
            "timestamp": datetime.fromtimestamp(v.timestamp, BRISBANE_TZ).strftime('%Y-%m-%d %H:%M:%S %Z') if v.HasField("timestamp") else "N/A"
        } for entity in feed.entity if entity.HasField("vehicle") for v in [entity.vehicle]
    ]
    return pd.DataFrame(vehicles)

def parse_trip_updates(content: bytes) -> pd.DataFrame:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    updates = []
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            tu = entity.trip_update
            if tu.stop_time_update:
                delay = tu.stop_time_update[0].arrival.delay
                status = "Delayed" if delay > 300 else ("Early" if delay < -60 else "On Time")
                updates.append({"trip_id": tu.trip.trip_id, "delay": delay, "status": status})
    return pd.DataFrame(updates)

def get_live_bus_data() -> tuple[pd.DataFrame, datetime]:
    """ Fetches data, using the cache if data is still fresh. """
    now = datetime.now(BRISBANE_TZ)
    
    # Check if cache is valid
    if CACHE["last_refreshed"] and (now - CACHE["last_refreshed"]).total_seconds() < REFRESH_INTERVAL_SECONDS:
        return CACHE["data"], CACHE["last_refreshed"]

    # --- Fetch new data ---
    vehicle_content = fetch_gtfs_rt(VEHICLE_POSITIONS_URL)
    trip_content = fetch_gtfs_rt(TRIP_UPDATES_URL)

    if not vehicle_content or not trip_content:
        # Return stale data if fetch fails, to avoid a blank page
        return CACHE["data"], CACHE.get("last_refreshed") or now

    vehicles_df = parse_vehicle_positions(vehicle_content)
    updates_df = parse_trip_updates(trip_content)

    if vehicles_df.empty:
        return CACHE["data"], CACHE.get("last_refreshed") or now

    live_data = vehicles_df.merge(updates_df, on="trip_id", how="left")
    live_data["delay"].fillna(0, inplace=True)
    live_data["status"].fillna("On Time", inplace=True)
    live_data["route_name"] = live_data["route_id"].str.split('-').str[0]

    def categorize_region(lat):
        if -27.75 <= lat <= -27.0: return "Brisbane"
        elif -28.2 <= lat <= -27.78: return "Gold Coast"
        elif -26.9 <= lat <= -26.3: return "Sunshine Coast"
        else: return "Other"
    live_data["region"] = live_data["lat"].apply(categorize_region)

    # Update cache
    CACHE["previous_data"] = CACHE["data"].copy()
    CACHE["data"] = live_data
    CACHE["last_refreshed"] = now
    
    return live_data, now

# --- Flask Route ---
@app.route('/')
def index():
    """ Main page route. """
    master_df, last_refreshed_time = get_live_bus_data()

    if master_df.empty:
        return "Could not retrieve live bus data at the moment. Please try again later.", 503

    # --- Handle Filters from URL query parameters ---
    # Example: /?region=Gold+Coast&route=700
    selected_region = request.args.get('region', 'Gold Coast')
    selected_route = request.args.get('route', '700')
    selected_status = request.args.getlist('status') # getlist for multiselect
    selected_vehicle = request.args.get('vehicle', 'All')

    # --- Cascading Filter Logic ---
    # Start with the full dataset and narrow it down
    df_for_region_options = master_df
    region_options = ["All"] + sorted(df_for_region_options["region"].unique().tolist())
    
    df_for_route_options = master_df[master_df["region"] == selected_region] if selected_region != "All" else master_df
    route_options = ["All"] + sorted(df_for_route_options["route_name"].unique().tolist())

    df_for_status_options = df_for_route_options[df_for_route_options["route_name"] == selected_route] if selected_route != "All" else df_for_route_options
    status_options = sorted(df_for_status_options["status"].unique().tolist())
    
    # If status is not selected, default to all available statuses
    if not selected_status:
        selected_status = status_options

    df_for_vehicle_options = df_for_status_options[df_for_status_options["status"].isin(selected_status)]
    vehicle_options = ["All"] + sorted(df_for_vehicle_options["vehicle_id"].unique().tolist())

    # --- Apply final filters to the data ---
    filtered_df = df_for_vehicle_options
    if selected_vehicle != "All":
        filtered_df = filtered_df[filtered_df["vehicle_id"] == selected_vehicle]

    # --- Merge with previous locations for animation ---
    previous_df = CACHE["previous_data"]
    if not previous_df.empty:
        prev_locations = previous_df[['vehicle_id', 'lat', 'lon']]
        filtered_df = filtered_df.merge(prev_locations, on='vehicle_id', how='left', suffixes=('', '_prev'))
    else:
        filtered_df['lat_prev'] = pd.NA
        filtered_df['lon_prev'] = pd.NA

    # --- Create Folium Map ---
    if not filtered_df.empty:
        map_center = [filtered_df['lat'].mean(), filtered_df['lon'].mean()]
        m = folium.Map(location=map_center, zoom_start=12, tiles="cartodbpositron")
        for _, row in filtered_df.iterrows():
            if pd.notna(row['lat_prev']) and (row['lat'] != row['lat_prev'] or row['lon'] != row['lon_prev']):
                AntPath(locations=[[row['lat_prev'], row['lon_prev']], [row['lat'], row['lon']]], color="blue", weight=5, delay=800, dash_array=[10, 20]).add_to(m)
            
            color = "red" if row['status'] == 'Delayed' else ("blue" if row['status'] == 'Early' else "green")
            popup_html = f"<b>Route:</b> {row['route_name']} ({row['route_id']})<br><b>Vehicle ID:</b> {row['vehicle_id']}<br><b>Status:</b> {row['status']}"
            folium.Marker([row['lat'], row['lon']], popup=folium.Popup(popup_html, max_width=300), icon=folium.Icon(color=color, icon="bus", prefix="fa")).add_to(m)
            
            label_text = f"vehicle: {row['vehicle_id']} on stop_seq: {row['stop_sequence']}"
            DivIcon(icon_size=(200, 36), icon_anchor=(85, 15), html=f'<div style="font-size: 10pt; font-weight: bold; color: {color}; background-color: #f5f5f5; padding: 4px 8px; border: 1px solid {color}; border-radius: 5px; box-shadow: 3px 3px 5px rgba(0,0,0,0.3); white-space: nowrap;">{label_text}</div>').add_to(folium.Marker(location=[row['lat'], row['lon']]))
        
        map_html = m._repr_html_()
    else:
        map_html = "<p>No buses match the current filter criteria.</p>"

    # --- Prepare data for rendering in the template ---
    context = {
        "tracked_buses_count": len(filtered_df),
        "last_refreshed": last_refreshed_time.strftime('%I:%M:%S %p %Z'),
        "next_refresh": (last_refreshed_time + timedelta(seconds=REFRESH_INTERVAL_SECONDS)).strftime('%I:%M:%S %p %Z'),
        "current_date": datetime.now(BRISBANE_TZ).strftime('%A, %d %B %Y'),
        "map_html": map_html,
        "region_options": region_options,
        "route_options": route_options,
        "status_options": status_options,
        "vehicle_options": vehicle_options,
        "selected_filters": {
            "region": selected_region,
            "route": selected_route,
            "status": selected_status,
            "vehicle": selected_vehicle
        },
        "refresh_interval": REFRESH_INTERVAL_SECONDS * 1000,
        "brisbane_tz_str": BRISBANE_TZ.zone
    }
    
    return render_template('index.html', **context)

if __name__ == '__main__':
    # For local testing
    app.run(debug=True)

