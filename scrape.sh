#!/bin/bash
# kmart.co.nz's storelocation sitemap lists Australian stores rather than NZ
# ones, so take the store list from the store locator's GraphQL API instead.
# Akamai blocks POSTs to the API from datacenter IPs but allows GETs; Apollo
# requires the preflight header on GET requests.
set -euo pipefail

QUERY='query getNearestLocations($lat: String!, $lon: String!, $distance: String!, $limit: Int) { nearestLocations(input: { lat: $lat, lon: $lon, distance: $distance, limit: $limit }) { locationId publicName phoneNumber address1 address2 address3 city state postcode latitude longitude tradingHours { hours weekDay } } }'
# Centre of NZ; 1500km covers the whole country without reaching Australia.
VARIABLES='{"lat":"-41.0","lon":"174.0","distance":"1500km","limit":500}'

TEMP_FILE=$(mktemp)
curl -sS -f -G 'https://api.kmart.co.nz/gateway/graphql' \
  -H 'apollo-require-preflight: true' \
  --data-urlencode "operationName=getNearestLocations" \
  --data-urlencode "query=${QUERY}" \
  --data-urlencode "variables=${VARIABLES}" \
  -o "$TEMP_FILE"

# Fail rather than overwrite the snapshot with an empty list
jq -e '.data.nearestLocations | length > 0' "$TEMP_FILE" > /dev/null
# The API lists trading hours starting from the current day, so sort them
# Monday to Sunday to keep the snapshot stable from one day to the next.
jq '["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"] as $days
  | .data.nearestLocations
  | map(.tradingHours |= (if . then sort_by(.weekDay as $d | $days | index($d) // 7) else . end))
  | sort_by(.locationId)' "$TEMP_FILE" > nz-store-locations.json
rm -f "$TEMP_FILE"
echo "Saved $(jq length nz-store-locations.json) stores to nz-store-locations.json"
