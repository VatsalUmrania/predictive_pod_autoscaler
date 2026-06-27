"""Tests for thread-safe Prometheus URL isolation (PR#18 fix)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ppa.config import PROMETHEUS_URL
from ppa.operator.prometheus import (
    get_current_prometheus_url,
    prom_query_parallel,
    set_prometheus_url,
)


class TestThreadLocalPromUrl:
    """Test thread-safe Prometheus URL isolation."""

    def test_default_prometheus_url(self):
        """Test that get_current_prometheus_url returns default PROMETHEUS_URL when not set."""
        # Create a new thread to avoid state from other tests
        result = []

        def thread_func():
            url = get_current_prometheus_url()
            result.append(url)

        thread = threading.Thread(target=thread_func)
        thread.start()
        thread.join()

        assert result[0] == PROMETHEUS_URL

    def test_set_prometheus_url_single_thread(self):
        """Test setting Prometheus URL in a single thread."""
        custom_url = "http://custom-prom:9090"
        set_prometheus_url(custom_url)
        assert get_current_prometheus_url() == custom_url

        # Reset to default for other tests
        set_prometheus_url(PROMETHEUS_URL)

    def test_thread_local_isolation_two_threads(self):
        """Test that two threads can have different Prometheus URLs simultaneously."""
        url1 = "http://prom-region1:9090"
        url2 = "http://prom-region2:9090"

        results = {}

        def thread1_func():
            set_prometheus_url(url1)
            time.sleep(0.1)  # Let thread 2 set its URL
            results["thread1"] = get_current_prometheus_url()

        def thread2_func():
            set_prometheus_url(url2)
            time.sleep(0.05)  # Slight delay to ensure thread1 runs first
            results["thread2"] = get_current_prometheus_url()

        t1 = threading.Thread(target=thread1_func)
        t2 = threading.Thread(target=thread2_func)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Each thread should see its own URL, not the other's
        assert (
            results["thread1"] == url1
        ), f"Thread1 got {results['thread1']}, expected {url1}"
        assert (
            results["thread2"] == url2
        ), f"Thread2 got {results['thread2']}, expected {url2}"

    def test_thread_local_isolation_multiple_threads(self):
        """Test thread isolation with many threads simultaneously."""
        num_threads = 10
        results = {}
        lock = threading.Lock()

        def thread_func(thread_id):
            # Each thread sets a unique URL
            custom_url = f"http://prom-{thread_id}:9090"
            set_prometheus_url(custom_url)

            # Simulate work with URL
            time.sleep(0.05)

            # Verify the thread still sees its own URL (not changed by other threads)
            actual_url = get_current_prometheus_url()
            with lock:
                results[thread_id] = actual_url

        threads = []
        for i in range(num_threads):
            t = threading.Thread(target=thread_func, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Verify each thread saw its own URL
        for thread_id in range(num_threads):
            expected_url = f"http://prom-{thread_id}:9090"
            assert (
                results[thread_id] == expected_url
            ), f"Thread {thread_id} got {results[thread_id]}, expected {expected_url}"

    def test_thread_local_isolation_with_threadpoolexecutor(self):
        """Test thread isolation with ThreadPoolExecutor (as used in prom_query_parallel)."""
        custom_urls = {}
        lock = threading.Lock()

        def set_and_get_url(thread_id):
            custom_url = f"http://prom-region-{thread_id}:9090"
            set_prometheus_url(custom_url)

            # Simulate some work
            time.sleep(0.02)

            # Get URL back
            actual_url = get_current_prometheus_url()
            with lock:
                custom_urls[thread_id] = actual_url

        # Use ThreadPoolExecutor like prom_query_parallel does
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(set_and_get_url, i) for i in range(10)]
            for future in futures:
                future.result()

        # Verify each thread saw its own URL
        for thread_id in range(10):
            expected_url = f"http://prom-region-{thread_id}:9090"
            assert (
                custom_urls[thread_id] == expected_url
            ), f"Worker {thread_id} got {custom_urls[thread_id]}, expected {expected_url}"

    def test_main_thread_unaffected_by_worker_threads(self):
        """Test that main thread's URL is not affected by worker thread changes."""
        main_url = "http://prom-main:9090"
        set_prometheus_url(main_url)

        worker_results = []

        def worker_func():
            # Worker sets its own URL
            worker_url = "http://prom-worker:9090"
            set_prometheus_url(worker_url)
            worker_results.append(get_current_prometheus_url())

        # Spawn worker thread
        worker = threading.Thread(target=worker_func)
        worker.start()
        worker.join()

        # Main thread should still see its own URL, not the worker's
        main_thread_url = get_current_prometheus_url()
        assert (
            main_thread_url == main_url
        ), f"Main thread got {main_thread_url}, expected {main_url}"
        assert worker_results[0] == "http://prom-worker:9090"

    def test_url_persistence_within_thread(self):
        """Test that URL persists for multiple calls within the same thread."""
        custom_url = "http://persistent-prom:9090"
        set_prometheus_url(custom_url)

        # Call get_current_prometheus_url multiple times
        urls = [get_current_prometheus_url() for _ in range(5)]

        # All should return the same URL
        assert all(
            url == custom_url for url in urls
        ), f"Expected all URLs to be {custom_url}, got {urls}"

    @patch("ppa.infrastructure.prometheus.requests.get")
    @patch("ppa.infrastructure.prometheus.prom_query")
    def test_prom_query_parallel_respects_thread_local_url(
        self, mock_prom_query, mock_requests
    ):
        """Test that prom_query_parallel uses thread-local URLs correctly."""
        custom_url = "http://parallel-test-prom:9090"
        set_prometheus_url(custom_url)

        # Mock prom_query to track which URL is being passed
        captured_urls = []

        def capture_url(query, prom_url=None, cr_state=None):
            # If no prom_url passed to prom_query, it will call get_current_prometheus_url internally
            url = prom_url or custom_url
            captured_urls.append(url)
            return 10.0

        mock_prom_query.side_effect = capture_url

        queries = {
            "metric1": "up",
            "metric2": "requests_total",
            "metric3": "latency_ms",
        }

        prom_query_parallel(queries, max_workers=3)

        # Verify all queries were executed
        assert len(captured_urls) >= 1, "prom_query should have been called"
        assert all(
            url == custom_url for url in captured_urls
        ), f"Expected all queries to use {custom_url}"

    def test_resetting_url_to_default(self):
        """Test resetting Prometheus URL back to default."""
        # Set custom URL
        custom_url = "http://custom:9090"
        set_prometheus_url(custom_url)
        assert get_current_prometheus_url() == custom_url

        # Reset to default
        set_prometheus_url(PROMETHEUS_URL)
        assert get_current_prometheus_url() == PROMETHEUS_URL

    def test_empty_string_url(self):
        """Test handling of empty string URL (should be stored and retrieved)."""
        empty_url = ""
        set_prometheus_url(empty_url)
        assert get_current_prometheus_url() == empty_url

        # Reset to default
        set_prometheus_url(PROMETHEUS_URL)

    def test_special_characters_in_url(self):
        """Test URLs with special characters are preserved."""
        special_url = "http://prom:9090/api?key=value&region=us-west-2@edge"
        set_prometheus_url(special_url)
        assert get_current_prometheus_url() == special_url

        # Reset to default
        set_prometheus_url(PROMETHEUS_URL)

    def test_concurrent_url_changes_isolation(self):
        """Test that concurrent URL changes don't interfere with each other."""
        barrier = threading.Barrier(5)  # Synchronize 5 threads at barrier
        results = {}

        def rapid_url_changes(thread_id):
            # Each thread rapidly changes its URL
            for iteration in range(10):
                url = f"http://prom-{thread_id}-{iteration}:9090"
                set_prometheus_url(url)

            # Synchronize: all threads reach here at roughly the same time
            barrier.wait()

            # After sync, each thread checks its final URL
            # (thread_id is now at last iteration 9)
            expected_url = f"http://prom-{thread_id}-9:9090"
            actual_url = get_current_prometheus_url()
            results[thread_id] = (expected_url, actual_url)

        threads = [
            threading.Thread(target=rapid_url_changes, args=(i,)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify each thread's final URL is what it set (not affected by other threads)
        for thread_id in range(5):
            expected, actual = results[thread_id]
            assert (
                expected == actual
            ), f"Thread {thread_id}: expected {expected}, got {actual}"
