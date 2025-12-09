from app.utils.haversine import haversine

lat1, lon1 = 16.062234, 108.238563 # Geocoded
lat2, lon2 = 16.0683, 108.2391     # MasterList

dist = haversine(lat1, lon1, lat2, lon2)
print(f"Distance: {dist} km")
print(f"Distance: {dist * 1000} meters")
