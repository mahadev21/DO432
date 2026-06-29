#!/usr/bin/env python3
"""
DO328 Service Mesh Traffic Generator
Ultra-simple script with YAML configuration support
"""

import sys
import os
import yaml
import argparse
import time
import subprocess
from collections import defaultdict, Counter
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import re
import json
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple, Optional, Any
import unicodedata

# Constants
DEFAULT_STRING_TRUNCATE_LENGTH = 35
DEFAULT_TABLE_COLUMN_WIDTHS = {
    "main_stats": [13, 20, 15, 15, 15],
    "per_item": [20, 9, 12, 11, 11],
    "response_dist": [35, 8, 15],
    "error_summary": [35, 20],
}
DEFAULT_TIMEOUT = 30
DEFAULT_SLEEP = 0.1


class TrafficGen:
    def __init__(self, config_path: str) -> None:
        self.config = self.load_config(config_path)
        self.stats: Dict[str, int] = defaultdict(int)
        self.responses: Counter = Counter()
        self.display_responses: Counter = Counter()
        self.times: List[float] = []
        self.errors: Counter = Counter()
        self._interrupted: bool = False
        self.item_stats: Dict[str, Dict[str, Any]] = {}  # Per-item stats for mix mode

    def load_config(self, config_path):
        """Load and merge config with defaults"""
        # Load defaults
        script_dir = os.path.dirname(os.path.abspath(__file__))
        defaults_path = os.path.join(script_dir, "defaults.yaml")
        if os.path.exists(defaults_path):
            with open(defaults_path) as f:
                defaults = yaml.safe_load(f)
        else:
            defaults = {}

        # Load lab config
        with open(config_path) as f:
            lab_config = yaml.safe_load(f)

        # Merge (lab config overrides defaults)
        return self.deep_merge(defaults, lab_config)

    def deep_merge(self, base, override):
        """Deep merge two dictionaries"""
        result = base.copy()
        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = self.deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def get_gateway_url(self):
        """Discover Service Mesh gateway URL or use configured gateway_url"""
        # Check if gateway_url is explicitly configured
        gateway_url = self.config.get("connection", {}).get("gateway_url")
        if gateway_url:
            return gateway_url

        # Fall back to dynamic discovery via OpenShift route
        try:
            gateway_config = self.config["connection"]["gateway"]
            namespace = gateway_config["namespace"]
            route_name = gateway_config["route_name"]

            cmd = [
                "oc",
                "get",
                "route",
                route_name,
                "-n",
                namespace,
                "-o",
                "jsonpath={.spec.host}",
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            if result.returncode == 0:
                host = result.stdout.strip()
                return f"http://{host}"
            else:
                print(f"❌ Failed to get gateway URL: {result.stderr}")
                return None
        except Exception as e:
            print(f"❌ Error discovering gateway: {e}")
            return None

    def normalize_content(self, content):
        """Strip dynamic content like transaction IDs"""
        if not self.config["analysis"]["content_normalization"]:
            return content

        # Strip transaction IDs, UUIDs, timestamps
        patterns = [
            (r"Transaction\s+id\s+\d+", ""),
            (r"UUID:\s*[a-f0-9\-]+", ""),
            (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ""),
        ]

        normalized = content.strip()
        for pattern, replacement in patterns:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

        return normalized.strip()

    def _extract_via_json_path(self, body: str, path: str):
        try:
            data = json.loads(body)
        except Exception:
            return "(bad-json)"
        # Normalize leading dot
        if path.startswith("."):
            path = path[1:]
        # Tokenize path: split by '.' but keep [index]
        tokens = []
        for part in path.split("."):
            # e.g., reviews[0][1]
            m = re.match(r"^([A-Za-z0-9_\-]+)(.*)$", part)
            if not m:
                return "(bad-path)"
            key = m.group(1)
            rest = m.group(2)
            tokens.append(key)
            # extract all [idx]
            for idx_m in re.finditer(r"\[(\d+)\]", rest):
                tokens.append(int(idx_m.group(1)))
        # Traverse
        cur = data
        for t in tokens:
            try:
                if isinstance(t, int):
                    cur = cur[t]
                else:
                    cur = cur[t]
            except Exception:
                return "(no-path)"
        # Convert terminal value to string like jq -r
        if isinstance(cur, (dict, list)):
            try:
                return json.dumps(cur, separators=(",", ":"))
            except Exception:
                return str(cur)
        return str(cur)

    def _get_display_config(self):
        """Safely retrieve display configuration as a dict"""
        display = self.config.get("display", {})
        return display if isinstance(display, dict) else {}

    def _get_display_width(self, text: str) -> int:
        """Calculate display width accounting for wide characters like emojis"""
        width = 0
        for char in text:
            # Use East Asian Width property to determine character width
            # 'W' (Wide) and 'F' (Fullwidth) are 2 columns
            # Most emojis are not in these categories but render as 2 columns
            # So we also check for common emoji ranges
            code = ord(char)
            ea_width = unicodedata.east_asian_width(char)

            # Check for emoji ranges that display as 2 columns
            # U+2600-U+27BF (Miscellaneous Symbols and Dingbats)
            # U+1F300-U+1F9FF (Emoticons and other symbols)
            is_emoji = (0x2600 <= code <= 0x27BF) or (0x1F300 <= code <= 0x1F9FF)

            if ea_width in ("W", "F") or is_emoji:
                width += 2
            else:
                width += 1
        return width

    def _pad_string(self, text: str, width: int, align: str = "left") -> str:
        """Pad string to specified display width accounting for wide characters"""
        current_width = self._get_display_width(text)
        if current_width >= width:
            return text

        padding_needed = width - current_width
        if align == "left":
            return text + (" " * padding_needed)
        elif align == "right":
            return (" " * padding_needed) + text
        else:  # center
            left_pad = padding_needed // 2
            right_pad = padding_needed - left_pad
            return (" " * left_pad) + text + (" " * right_pad)

    def _get_char_width(self, char: str) -> int:
        """Get display width of a single character"""
        code = ord(char)
        ea_width = unicodedata.east_asian_width(char)
        is_emoji = (0x2600 <= code <= 0x27BF) or (0x1F300 <= code <= 0x1F9FF)
        return 2 if (ea_width in ("W", "F") or is_emoji) else 1

    def _truncate_string(
        self, text: Any, max_length: int = DEFAULT_STRING_TRUNCATE_LENGTH
    ) -> str:
        """Truncate a string to max_length display width, adding '...' if needed"""
        text_str = str(text)
        current_width = 0
        truncate_pos = 0
        needs_truncation = False

        for i, char in enumerate(text_str):
            char_width = self._get_char_width(char)
            # Check if adding this character would exceed the limit
            if current_width + char_width > max_length:
                needs_truncation = True
                break
            current_width += char_width
            truncate_pos = i + 1

        if needs_truncation:
            # Truncate and add "..." making sure total doesn't exceed max_length
            # Reserve 3 chars for "..."
            truncate_width = max_length - 3
            current_width = 0
            truncate_pos = 0
            for i, char in enumerate(text_str):
                char_width = self._get_char_width(char)
                if current_width + char_width > truncate_width:
                    break
                current_width += char_width
                truncate_pos = i + 1
            return text_str[:truncate_pos] + "..."

        return text_str

    def _print_section_header(self, title):
        """Print a section header with separator line"""
        print(f"\n{title}")
        print("=" * 80)

    def _format_progress_payload(self, result):
        """Decide what to show on progress lines based on display.response_view"""
        display_cfg = self._get_display_config()
        view = (display_cfg.get("response_view") or "body").lower()
        if view == "status":
            status_code = result.get("status")
            return f"HTTP {status_code}"
        if view == "extract":
            json_path = display_cfg.get("extract_json_path")
            if json_path:
                extracted = self._extract_via_json_path(
                    result.get("content", "") or "", json_path
                )
                # Replace "(no-path)" with "No stars" for successful HTTP 200
                if extracted == "(no-path)" and result.get("status") == 200:
                    return "No stars"
                return extracted
            regex = display_cfg.get("extract_regex")
            body = result.get("content", "") or ""
            if regex:
                try:
                    m = re.search(regex, body)
                    if m:
                        return m.group(0)
                    return "(no-match)"
                except re.error as _:
                    return "(bad-regex)"
            return "(no-regex)"
        if view == "smart_json":
            # Try to parse and display key fields intelligently
            body = result.get("content", "") or ""
            try:
                data = json.loads(body)

                # Check for reviews array structure (primary use case)
                reviews = data.get("reviews", [])
                if reviews and isinstance(reviews, list) and len(reviews) > 0:
                    # Check if first review has a rating field
                    rating = reviews[0].get("rating")
                    if rating is not None:
                        # reviews-v2 or reviews-v3 with rating
                        # Check if rating contains an error
                        if "error" in rating:
                            return f"Ratings: ❌ {rating['error']}"
                        # Check if rating contains color (success case)
                        if "color" in rating:
                            return f"Ratings: ✅ {rating['color']}"
                    else:
                        # reviews-v1 (has reviews array but no rating field)
                        return "No stars"

                # Fallback patterns for other response structures
                # Top-level error
                if "error" in data:
                    return f"Ratings: ❌ {data['error']}"
                # Direct rating object (stars + color at top level)
                if "stars" in data and "color" in data:
                    return f"Ratings: ✅ {data['color']}"

                # Fallback: return truncated JSON
                return self._truncate_string(json.dumps(data, separators=(",", ":")))
            except json.JSONDecodeError:
                return "(invalid-json)"
        # default/body
        return result.get("normalized", result.get("error", ""))

    def _format_result_components(self, result):
        """Format result into status emoji, http_status string, and payload"""
        status = "✅" if result["success"] else "❌"
        payload = self._format_progress_payload(result)
        http_status = f"HTTP {result['status']}" if result["status"] > 0 else "HTTP 0"
        return status, http_status, payload

    def _format_time_ms(self, time_seconds):
        """Format time in seconds to milliseconds string"""
        return f"{time_seconds * 1000:.1f}ms"

    def _calculate_percentage(self, part, total):
        """Calculate percentage, returning 0 if total is 0"""
        return (part / total * 100) if total else 0.0

    def _should_show_progress(self) -> bool:
        """Check if progress should be shown based on display config"""
        display_cfg = self._get_display_config()
        return display_cfg.get("show_progress", True) and not display_cfg.get(
            "quiet_mode", False
        )

    def _print_request_result(
        self,
        result: Dict[str, Any],
        request_num: int,
        total_count: Optional[int] = None,
    ) -> None:
        """Print a single request result in a consistent format"""
        status, http_status, payload = self._format_result_components(result)

        if total_count is not None:
            counter_str = f"[{request_num}/{total_count}]"
        else:
            counter_str = f"[{request_num}]"

        print(
            f"{counter_str} {status} {http_status} -- {payload} "
            f"({self._format_time_ms(result['time'])})"
        )

    def _track_result(self, result_obj, success=True, error_msg=None):
        """Track statistics and display response for a request result"""
        self.stats["total"] += 1
        self.times.append(result_obj["time"])

        if success:
            self.stats["success"] += 1
            if "normalized" in result_obj:
                self.responses[result_obj["normalized"]] += 1
        else:
            self.stats["failed"] += 1
            if error_msg:
                self.errors[error_msg] += 1

        label = self._format_progress_payload(result_obj)
        self.display_responses[label] += 1

    def _create_error_result(self, start_time, error_msg, status=0):
        """Create an error result object"""
        result_obj = {
            "success": False,
            "error": error_msg,
            "time": time.time() - start_time,
            "status": status,
        }
        self._track_result(result_obj, success=False, error_msg=error_msg)
        return result_obj

    def make_request(self, url, headers=None, sleep_after=0):
        """Make a single HTTP request and optionally sleep after"""
        start_time = time.time()

        try:
            req = Request(url)
            if headers:
                for key, value in headers.items():
                    req.add_header(key, value)

            timeout = self.config["traffic"]["options"]["timeout"]
            response = urlopen(req, timeout=timeout)
            content = response.read().decode("utf-8")
            elapsed_time = time.time() - start_time

            normalized = self.normalize_content(content)
            result_obj = {
                "success": True,
                "content": content,
                "normalized": normalized,
                "time": elapsed_time,
                "status": response.getcode(),
            }
            self._track_result(result_obj, success=True)

            if sleep_after > 0:
                time.sleep(sleep_after)

            return result_obj

        except HTTPError as e:
            # Read error response body
            try:
                error_content = e.read().decode("utf-8")
            except (AttributeError, UnicodeDecodeError):
                error_content = ""

            elapsed_time = time.time() - start_time
            normalized = self.normalize_content(error_content)
            result_obj = {
                "success": False,
                "content": error_content,
                "normalized": normalized,
                "error": f"HTTP {e.code}",
                "time": elapsed_time,
                "status": e.code,
            }
            self._track_result(result_obj, success=False, error_msg=f"HTTP {e.code}")
            return result_obj

        except Exception as e:
            return self._create_error_result(start_time, str(e), 0)

    def _extract_scenario_params(self, scenario):
        """Extract common scenario parameters with defaults"""
        return {
            "endpoint": scenario.get("endpoint", self.config["traffic"]["endpoint"]),
            "headers": scenario.get("headers", {}).get("default", {}),
            "count": scenario.get("count", self.config["traffic"].get("count")),
            "duration": scenario.get(
                "duration", self.config["traffic"].get("duration")
            ),
            "sleep": scenario.get("sleep", self.config["traffic"]["sleep"]),
        }

    def _generate_curl_command(self, url, headers=None):
        """Generate curl command equivalent to the request being made"""
        parts = [f"curl -s {url}"]

        if headers:
            parts.extend(f"-H '{key}: {value}'" for key, value in headers.items())

        return " ".join(parts)

    def _setup_scenario_url(self, scenario):
        """Setup URL and parameters for scenario, None if gateway unavailable"""
        base_url = self.get_gateway_url()
        if not base_url:
            return None

        params = self._extract_scenario_params(scenario)
        url = base_url + params["endpoint"]
        return url, params

    def _execute_batch_requests(
        self,
        url: str,
        headers: Dict[str, str],
        count: int,
        sleep_duration: float,
        num_workers: int,
        request_offset: int = 0,
        total_count: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Execute batch of requests (parallel/sequential based on workers)"""
        if num_workers > 1:
            # Parallel execution using ThreadPoolExecutor with progressive display
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [
                    executor.submit(self.make_request, url, headers, sleep_duration)
                    for _ in range(count)
                ]

                # Collect results and display progress as they complete
                results = []
                if self._should_show_progress():
                    for i, future in enumerate(futures, 1):
                        result = future.result()
                        results.append(result)
                        self._print_request_result(
                            result, request_offset + i, total_count
                        )
                else:
                    # If not showing progress, collect all results at once
                    results = [future.result() for future in futures]
        else:
            # Sequential execution
            results = []
            for i in range(count):
                result = self.make_request(url, headers, sleep_duration)
                results.append(result)

                # Display progress immediately for sequential execution
                if self._should_show_progress():
                    self._print_request_result(
                        result, request_offset + i + 1, total_count
                    )

        return results

    def _normalize_mix_items(self, items):
        """Normalize mix mode items and initialize per-item stats"""
        self.item_stats = {}
        normalized_items = []

        for idx, item in enumerate(items):
            name = item.get("name") or f"item-{idx + 1}"
            endpoint = item.get("endpoint", self.config["traffic"]["endpoint"])
            headers = item.get("headers", {}).get("default", {})
            weight = int(item.get("weight", 1))

            self.item_stats[name] = {
                "requests": 0,
                "success": 0,
                "times": [],
            }
            normalized_items.append(
                {
                    "name": name,
                    "endpoint": endpoint,
                    "headers": headers,
                    "weight": weight,
                }
            )

        return normalized_items

    def _update_item_stats(self, item_name, result):
        """Update statistics for a specific item in mix mode"""
        stats = self.item_stats[item_name]
        stats["requests"] += 1
        if result["success"]:
            stats["success"] += 1
        stats["times"].append(result["time"])

    def _execute_batch_mix_requests(
        self,
        items_with_names: List[Tuple[Dict[str, Any], str]],
        base_url: str,
        num_workers: int,
        sleep_duration: float,
        request_offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Execute batch of mix requests (parallel/sequential based on workers)"""
        if num_workers > 1:
            # Parallel execution using ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                # Submit all tasks
                futures_to_items = {}
                for item, item_name in items_with_names:
                    url = base_url + item["endpoint"]
                    future = executor.submit(
                        self.make_request, url, item["headers"], sleep_duration
                    )
                    futures_to_items[future] = (item, item_name)

                # Collect results and update stats with progressive display
                results = []
                for idx, (future, (item, item_name)) in enumerate(
                    futures_to_items.items(), 1
                ):
                    result = future.result()
                    results.append(result)
                    self._update_item_stats(item_name, result)

                    if self._should_show_progress():
                        status, http_status, payload = self._format_result_components(
                            result
                        )
                        print(
                            f"[{request_offset + idx}] {item_name} {status} "
                            f"{http_status} -- {payload} "
                            f"({self._format_time_ms(result['time'])})"
                        )
        else:
            # Sequential execution
            results = []
            for idx, (item, item_name) in enumerate(items_with_names, 1):
                url = base_url + item["endpoint"]
                result = self.make_request(url, item["headers"], 0)  # No sleep
                results.append(result)

                self._update_item_stats(item_name, result)
                if self._should_show_progress():
                    status, http_status, payload = self._format_result_components(
                        result
                    )
                    print(
                        f"[{request_offset + idx}] {item_name} {status} "
                        f"{http_status} -- {payload} "
                        f"({self._format_time_ms(result['time'])})"
                    )

                # Sleep after displaying progress (except for the last request)
                if sleep_duration > 0 and idx < len(items_with_names):
                    time.sleep(sleep_duration)

        return results

    def _handle_interrupt(self, mode_name):
        """Handle keyboard interrupt and set interrupted flag"""
        self._interrupted = True
        print(f"\n⏸️  Interrupted by user (Ctrl+C). Stopping {mode_name}...")

    def _setup_and_print_scenario(
        self, scenario, mode_emoji, mode_desc, count_or_duration
    ):
        """Setup scenario URL and print mode info with curl command"""
        setup = self._setup_scenario_url(scenario)
        if not setup:
            return None

        url, params = setup
        print(f"{mode_emoji} {mode_desc}: {count_or_duration} to {url}")
        print(f"🔗 {self._generate_curl_command(url, params['headers'])}")
        return url, params

    def run_finite(self, scenario):
        """Run finite number of requests"""
        result = self._setup_and_print_scenario(
            scenario,
            "🎯",
            "Finite mode",
            f"{self._extract_scenario_params(scenario)['count']} requests",
        )
        if not result:
            return
        url, params = result

        # Get parallel_workers configuration
        # parallel_workers = self.config["traffic"]["options"].get("parallel_workers",1)

        try:
            # Execute all requests in one batch (parallel or sequential)
            self._execute_batch_requests(
                url,
                params["headers"],
                params["count"],
                params["sleep"],
                1,  # ignore parallel_workers parameter
                request_offset=0,
                total_count=params["count"],
            )
        except KeyboardInterrupt:
            self._handle_interrupt("finite mode")
            return

    def run_continuous(self, scenario):
        """Run continuous requests for duration"""
        result = self._setup_and_print_scenario(
            scenario,
            "🔄",
            "Continuous mode",
            f"{self._extract_scenario_params(scenario)['duration']}s",
        )
        if not result:
            return
        url, params = result

        # Get parallel_workers configuration
        parallel_workers = self.config["traffic"]["options"].get("parallel_workers", 1)

        start_time = time.time()
        count = 0

        try:
            # Execute batches of requests until duration expires
            while time.time() - start_time < params["duration"]:
                self._execute_batch_requests(
                    url,
                    params["headers"],
                    parallel_workers,
                    params["sleep"],
                    parallel_workers,
                    request_offset=count,
                )
                count += parallel_workers
        except KeyboardInterrupt:
            self._handle_interrupt("continuous mode")
            return

    def run_multi(self):
        """Run multiple scenarios in sequence"""
        print(
            f"🚀 Multi-scenario mode: "
            f"{len(self.config['traffic']['scenarios'])} scenarios"
        )

        for i, scenario in enumerate(self.config["traffic"]["scenarios"]):
            if self._interrupted:
                break
            scenario_name = scenario.get("name", f"Scenario {i + 1}")
            print(f"\n📋 Running: {scenario_name}")

            mode = scenario.get("mode", "finite")
            try:
                if mode == "finite":
                    self.run_finite(scenario)
                elif mode == "continuous":
                    self.run_continuous(scenario)
            except KeyboardInterrupt:
                self._handle_interrupt("multi-scenario mode")
                break

    def run_mix(self):
        """Run interleaved requests across items using a selection pattern"""
        traffic_cfg = self.config["traffic"]
        items = traffic_cfg.get("items", [])
        if not items:
            print("⚠️  No items defined for mix mode. Nothing to run.")
            return
        pattern = (traffic_cfg.get("pattern") or "round-robin").lower()
        sleep = traffic_cfg.get("sleep", self.config["traffic"]["sleep"])
        base_url = self.get_gateway_url()
        if not base_url:
            return

        # Get parallel_workers configuration
        parallel_workers = self.config["traffic"]["options"].get("parallel_workers", 1)

        normalized_items = self._normalize_mix_items(items)

        def pick_index_rr(counter: int) -> int:
            return counter % len(normalized_items)

        weighted_pool = []
        if pattern == "weighted":
            for idx, it in enumerate(normalized_items):
                weighted_pool.extend([idx] * max(1, it["weight"]))
            if not weighted_pool:
                weighted_pool = list(range(len(normalized_items)))

        def pick_index(counter: int) -> int:
            if pattern == "round-robin":
                return pick_index_rr(counter)
            if pattern == "random":
                return random.randint(0, len(normalized_items) - 1)
            if pattern == "weighted":
                return random.choice(weighted_pool)
            # default
            return pick_index_rr(counter)

        count = traffic_cfg.get("count")
        duration = traffic_cfg.get("duration")

        # Show curl commands for each item
        print(f"🧪 Mix mode: pattern={pattern}, items={len(normalized_items)}")
        for it in normalized_items:
            url = base_url + it["endpoint"]
            print(
                f"🔗 {it['name']}: "
                f"{self._generate_curl_command(url, it['headers'])}"
            )

        start_time = time.time()
        counter = 0
        try:
            if count is not None:
                # Finite total requests across all items
                # Build all items to request
                items_to_request = []
                for req in range(1, int(count) + 1):
                    idx = pick_index(counter)
                    items_to_request.append(
                        (normalized_items[idx], normalized_items[idx]["name"])
                    )
                    counter += 1

                # Execute all requests as one batch
                self._execute_batch_mix_requests(
                    items_to_request,
                    base_url,
                    parallel_workers,
                    sleep,
                    request_offset=0,
                )
            elif duration is not None:
                # Continuous for duration seconds - execute in batches
                while time.time() - start_time < float(duration):
                    # Build a batch of requests
                    items_to_request = []
                    for _ in range(parallel_workers):
                        if time.time() - start_time >= float(duration):
                            break
                        idx = pick_index(counter)
                        items_to_request.append(
                            (
                                normalized_items[idx],
                                normalized_items[idx]["name"],
                            )
                        )
                        counter += 1

                    if items_to_request:
                        self._execute_batch_mix_requests(
                            items_to_request,
                            base_url,
                            parallel_workers,
                            sleep,
                            request_offset=counter - len(items_to_request),
                        )
            else:
                print(
                    "⚠️ Mix mode requires either 'count' or 'duration' "
                    "in traffic config."
                )
        except KeyboardInterrupt:
            self._handle_interrupt("mix mode")
            return

    def _calculate_timing_stats(self, times):
        """Calculate avg, p50, p95 from list of times (seconds), return ms"""
        if not times:
            return 0.0, 0.0, 0.0

        avg = sum(times) / len(times) * 1000
        if len(times) >= 2:
            sorted_times = sorted(times)
            p50 = sorted_times[len(sorted_times) // 2] * 1000
            p95 = sorted_times[int(len(sorted_times) * 0.95)] * 1000
        else:
            p50 = 0.0
            p95 = 0.0

        return avg, p50, p95

    def _print_table(self, headers, rows, col_widths):
        """Print a formatted table with headers and rows"""
        # Build separator based on column widths
        top_border = "┌" + "┬".join("─" * (w + 2) for w in col_widths) + "┐"
        middle_border = "├" + "┼".join("─" * (w + 2) for w in col_widths) + "┤"
        bottom_border = "└" + "┴".join("─" * (w + 2) for w in col_widths) + "┘"

        # Print top border
        print(top_border)

        # Print headers
        header_cells = [f" {h:<{w}} " for h, w in zip(headers, col_widths)]
        print("│" + "│".join(header_cells) + "│")

        # Print middle border
        print(middle_border)

        # Print rows
        for row in rows:
            print(row)

        # Print bottom border
        print(bottom_border)

    def print_stats(self):
        """Print final statistics in horizontal table format"""
        if not self.config["analysis"]["enabled"]:
            return

        self._print_section_header("📊 Traffic Statistics")

        if self.stats["total"] > 0:
            success_rate = self._calculate_percentage(
                self.stats["success"], self.stats["total"]
            )
            avg_time, p50, p95 = self._calculate_timing_stats(self.times)

            # Main statistics table
            headers = ["Total Request", "Success Rate", "Average", "P50", "P95"]
            col_widths = DEFAULT_TABLE_COLUMN_WIDTHS["main_stats"]
            success_rate_text = (
                f"{success_rate:>6.1f}% ({self.stats['success']}/{self.stats['total']})"
            )
            row = (
                f"│ {self.stats['total']:<13} │ "
                f"{success_rate_text:^20} │ "
                f"{avg_time:>13.1f}ms │ {p50:>13.1f}ms │ {p95:>13.1f}ms │"
            )
            self._print_table(headers, [row], col_widths)

            # Per-item statistics for mix mode
            if self.item_stats:
                self._print_section_header("📊 Per-Item Statistics")
                headers = [
                    "Item",
                    "Requests",
                    "Success Rate",
                    "Avg (ms)",
                    "P95 (ms)",
                ]
                col_widths = DEFAULT_TABLE_COLUMN_WIDTHS["per_item"]
                rows = []
                for name, st in self.item_stats.items():
                    reqs = st["requests"]
                    succ = st["success"]
                    rate = self._calculate_percentage(succ, reqs)
                    avg, _, p95i = self._calculate_timing_stats(st["times"])
                    rows.append(
                        f"│ {name:<20} │ {reqs:<9} │ {rate:>6.1f}%      │ "
                        f"{avg:>8.1f}   │ {p95i:>8.1f}   │"
                    )
                self._print_table(headers, rows, col_widths)

            # Show response distribution if expected responses defined
            if "expected_responses" in self.config.get("analysis", {}):
                self._print_section_header("📈 Response Distribution")
                headers = ["Response", "Count", "Percentage"]
                col_widths = DEFAULT_TABLE_COLUMN_WIDTHS["response_dist"]
                rows = []
                dist_counter = self.display_responses or self.responses
                total_responses = sum(dist_counter.values())
                for response, count in dist_counter.most_common():
                    percentage = self._calculate_percentage(count, total_responses)
                    response_display = self._truncate_string(
                        response, DEFAULT_STRING_TRUNCATE_LENGTH
                    )
                    col_w = DEFAULT_TABLE_COLUMN_WIDTHS["response_dist"][0]
                    count_w = DEFAULT_TABLE_COLUMN_WIDTHS["response_dist"][1]
                    pct_w = DEFAULT_TABLE_COLUMN_WIDTHS["response_dist"][2]

                    # Pad strings accounting for emoji display width
                    response_padded = self._pad_string(response_display, col_w, "left")
                    count_str = str(count)
                    count_padded = self._pad_string(count_str, count_w, "right")
                    percentage_str = f"{percentage:.1f}%"
                    pct_padded = self._pad_string(percentage_str, pct_w, "right")

                    row = f"│ {response_padded} │ {count_padded} │ {pct_padded} │"
                    rows.append(row)
                self._print_table(headers, rows, col_widths)

            # Show errors table if any
            if self.errors:
                self._print_section_header("❌ Error Summary")
                headers = ["Error Type", "Count"]
                col_widths = DEFAULT_TABLE_COLUMN_WIDTHS["error_summary"]
                rows = []
                for error, count in self.errors.most_common():
                    error_display = self._truncate_string(
                        error, DEFAULT_STRING_TRUNCATE_LENGTH
                    )
                    col_w = DEFAULT_TABLE_COLUMN_WIDTHS["error_summary"][0]
                    count_w = DEFAULT_TABLE_COLUMN_WIDTHS["error_summary"][1]

                    # Pad strings accounting for emoji display width
                    error_padded = self._pad_string(error_display, col_w, "left")
                    count_str = str(count)
                    count_padded = self._pad_string(count_str, count_w, "left")

                    row = f"│ {error_padded} │ {count_padded} │"
                    rows.append(row)
                self._print_table(headers, rows, col_widths)

    def run(self):
        """Main execution"""
        mode = self.config["traffic"]["mode"]

        if mode == "finite":
            self.run_finite(self.config["traffic"])
        elif mode == "continuous":
            self.run_continuous(self.config["traffic"])
        elif mode == "multi":
            self.run_multi()
        elif mode in ("mix", "combo"):
            self.run_mix()
        else:
            print(f"❌ Unknown mode: {mode}")
            return

        self.print_stats()


if __name__ == "__main__":

    def main():
        parser = argparse.ArgumentParser(
            description="DO328 Service Mesh Traffic Generator"
        )
        parser.add_argument("config", help="Path to traffic config YAML file")
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be executed"
        )

        args = parser.parse_args()

        if not os.path.exists(args.config):
            print(f"❌ Config file not found: {args.config}")
            sys.exit(1)

        try:
            gen = TrafficGen(args.config)

            if args.dry_run:
                print("🔍 DRY RUN - Configuration:")
                print(yaml.dump(gen.config, default_flow_style=False))
            else:
                gen.run()

        except Exception as e:
            print(f"❌ Error: {e}")
            sys.exit(1)

    main()
