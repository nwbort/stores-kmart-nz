import json
import random
import sys
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

LOCATIONS_FILE = "nz-store-locations.json"
STORE_DETAIL_URL = "https://www.kmart.co.nz/store-detail/{slug}/"
# Kmart's WAF starts returning 403 when every store is fetched in a burst,
# so keep concurrency modest and back off hard when throttled.
DEFAULT_WORKERS = 8
FETCH_ATTEMPTS = 5
# Statuses worth retrying: throttling and transient server-side faults. A 404
# will not become a 200 on retry.
RETRYABLE_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}
# Allow the odd store page to fail - but fail loudly if the success rate collapses.
DEFAULT_MIN_SUCCESS_RATE = 0.9
verbose = False

def store_url(public_name):
    """Build a store page URL the same way kmart.co.nz's store locator does."""
    # The site lowercases publicName and joins on single spaces, so a trailing
    # space (e.g. "Sylvia Park Nz ") becomes a trailing hyphen.
    return STORE_DETAIL_URL.format(slug='-'.join(public_name.lower().split(' ')))

def extract_urls_from_locations(filepath):
    """Build store URLs from the store locator API snapshot."""
    try:
        with open(filepath) as f:
            locations = json.load(f)
        return [store_url(loc['publicName']) for loc in locations if loc.get('publicName')]
    except Exception as e:
        print(f"Error parsing locations: {e}", file=sys.stderr)
        return []

def backoff_delay(attempt, retry_after=None):
    """Seconds to wait before the next attempt, with jitter to desynchronise workers."""
    if retry_after:
        try:
            return min(float(retry_after), 30)
        except ValueError:
            pass
    return min(2 ** attempt, 16) + random.uniform(0, 1)

def fetch_html(url):
    """Fetch a page, retrying on throttling and transient network errors."""
    # Note: Kmart's WAF 403s a browser User-Agent from a datacenter IP
    # but serves urllib's default one, so do not set one here.
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urlopen(url, timeout=15) as response:
                return response.read().decode('utf-8')
        except HTTPError as e:
            if e.code not in RETRYABLE_STATUSES or attempt == FETCH_ATTEMPTS - 1:
                raise
            delay = backoff_delay(attempt, e.headers.get('Retry-After'))
            if verbose:
                print(f"Retrying {url} in {delay:.1f}s after HTTP {e.code}", file=sys.stderr)
            time.sleep(delay)
        except (URLError, TimeoutError, OSError) as e:
            if attempt == FETCH_ATTEMPTS - 1:
                raise
            delay = backoff_delay(attempt)
            if verbose:
                print(f"Retrying {url} in {delay:.1f}s after error: {e}", file=sys.stderr)
            time.sleep(delay)

def get_store_details(url):
    """Fetch a store page and extract details from the JSON data."""
    try:
        if verbose:
            print(f"Fetching: {url}", file=sys.stderr)

        html = fetch_html(url)

        # Extract JSON from __NEXT_DATA__ script tag
        start_marker = '"__NEXT_DATA__":'
        start_idx = html.find(start_marker)
        
        if start_idx == -1:
            # Try alternate marker
            start_marker = 'id="__NEXT_DATA__"'
            start_idx = html.find(start_marker)
            if start_idx == -1:
                return None
            # Find the JSON content after the tag
            start_idx = html.find('>', start_idx) + 1
            end_idx = html.find('</script>', start_idx)
        else:
            start_idx += len(start_marker)
            # Find the end of the JSON object
            brace_count = 0
            in_string = False
            escape_next = False
            end_idx = start_idx
            
            for i, char in enumerate(html[start_idx:], start=start_idx):
                if escape_next:
                    escape_next = False
                    continue
                    
                if char == '\\':
                    escape_next = True
                    continue
                    
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                
                if not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i + 1
                            break
        
        json_str = html[start_idx:end_idx]
        data = json.loads(json_str)
        
        # Navigate to the location data
        location = data.get('props', {}).get('pageProps', {}).get('location', {})
        
        if not location:
            return None
        
        store_data = {
            'locationId': location.get('locationId'),
            'publicName': location.get('publicName'),
            'phoneNumber': location.get('phoneNumber'),
            'address1': location.get('address1'),
            'address2': location.get('address2'),
            'address3': location.get('address3'),
            'city': location.get('city'),
            'state': location.get('state'),
            'postcode': location.get('postcode'),
            'latitude': location.get('latitude'),
            'longitude': location.get('longitude'),
            'tradingHours': location.get('tradingHours'),
            'typename': location.get('__typename'),
            'url': url
        }
        
        return store_data
        
    except (URLError, HTTPError) as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON from {url}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error processing {url}: {e}", file=sys.stderr)
        return None

def sort_trading_hours(store_data):
    """Sort trading hours from Monday to Sunday."""
    if store_data and 'tradingHours' in store_data and store_data['tradingHours']:
        day_order = {
            'MONDAY': 0,
            'TUESDAY': 1,
            'WEDNESDAY': 2,
            'THURSDAY': 3,
            'FRIDAY': 4,
            'SATURDAY': 5,
            'SUNDAY': 6
        }
        store_data['tradingHours'] = sorted(
            store_data['tradingHours'],
            key=lambda x: day_order.get(x.get('weekDay', ''), 7)
        )
    return store_data

def main():
    """Main function to scrape all stores and output as JSON."""
    global verbose
    
    parser = argparse.ArgumentParser(description='Extract Kmart NZ store details from the store locator list.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    parser.add_argument('-w', '--workers', type=int, default=DEFAULT_WORKERS, help=f'Number of parallel workers (default: {DEFAULT_WORKERS})')
    parser.add_argument('--min-success-rate', type=float, default=DEFAULT_MIN_SUCCESS_RATE,
                        help=f'Exit non-zero if the fraction of stores extracted falls below this (default: {DEFAULT_MIN_SUCCESS_RATE})')
    args = parser.parse_args()
    verbose = args.verbose
    
    if not Path(LOCATIONS_FILE).exists():
        print(f"Error: Locations file '{LOCATIONS_FILE}' not found.", file=sys.stderr)
        sys.exit(1)
    
    urls = extract_urls_from_locations(LOCATIONS_FILE)

    if verbose:
        print(f"Found {len(urls)} stores in locations file", file=sys.stderr)

    if not urls:
        print(f"Error: No store URLs found in '{LOCATIONS_FILE}'.", file=sys.stderr)
        sys.exit(1)

    all_stores = []
    errors = []
    
    # Use thread pool for parallel fetching
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        # Submit all tasks
        future_to_url = {
            executor.submit(get_store_details, url): (i, url) 
            for i, url in enumerate(urls, 1)
        }
        
        # Process results as they complete
        for future in as_completed(future_to_url):
            i, url = future_to_url[future]
            try:
                store_data = future.result()
                if store_data:
                    store_data = sort_trading_hours(store_data)
                    all_stores.append(store_data)
                    if verbose:
                        print(f"  [{i}/{len(urls)}] {store_data.get('publicName', 'Unknown')}", 
                              file=sys.stderr)
                else:
                    errors.append((i, url))
                    if verbose:
                        print(f"  [{i}/{len(urls)}] Failed to extract", file=sys.stderr)
            except Exception as e:
                errors.append((i, url))
                print(f"Error processing {url}: {e}", file=sys.stderr)
    
    # Print summary
    print(f"Extracted {len(all_stores)} stores", file=sys.stderr)
    if errors:
        print(f"Failed to extract {len(errors)} stores:", file=sys.stderr)
        for idx, url in errors:
            print(f"  [{idx}] {url}", file=sys.stderr)

    success_rate = len(all_stores) / len(urls)
    if success_rate < args.min_success_rate:
        print(
            f"Error: Only extracted {len(all_stores)}/{len(urls)} stores "
            f"({success_rate:.1%}), below the {args.min_success_rate:.1%} threshold.",
            file=sys.stderr,
        )
        sys.exit(1)

    all_stores_sorted = sorted(all_stores, key=lambda x: x.get('locationId', ''))
    print(json.dumps(all_stores_sorted, indent=2))

if __name__ == "__main__":
    main()
