def is_inside_bounds(lat: float, lng: float, south: float, west: float, north: float, east: float) -> bool:
    return south <= lat <= north and west <= lng <= east


GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lng: float, precision: int) -> str:
    """The standard geohash: every step halves the longitude range, then the latitude range, and five bits make a character."""
    lat_low, lat_high, lng_low, lng_high = -90.0, 90.0, -180.0, 180.0
    characters = []
    bits = 0
    bit_count = 0
    use_lng = True
    while len(characters) < precision:
        if use_lng:
            middle = (lng_low + lng_high) / 2
            if lng >= middle:
                bits = bits * 2 + 1
                lng_low = middle
            else:
                bits = bits * 2
                lng_high = middle
        else:
            middle = (lat_low + lat_high) / 2
            if lat >= middle:
                bits = bits * 2 + 1
                lat_low = middle
            else:
                bits = bits * 2
                lat_high = middle
        use_lng = not use_lng
        bit_count += 1
        if bit_count == 5:
            characters.append(GEOHASH_ALPHABET[bits])
            bits = 0
            bit_count = 0
    return "".join(characters)
