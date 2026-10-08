def is_inside_bounds(lat: float, lng: float, south: float, west: float, north: float, east: float) -> bool:
    return south <= lat <= north and west <= lng <= east
