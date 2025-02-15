# Copyright (c) 2023 The ARA Records Ansible authors
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import logging
import sys
import time
from datetime import datetime, timedelta

from cliff.command import Command

import ara.cli.utils as cli_utils
from ara.cli.base import global_arguments
from ara.clients.utils import get_client

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    HAS_PROMETHEUS_CLIENT = True
except ImportError:
    HAS_PROMETHEUS_CLIENT = False

def get_search_results(client, kind, limit, created_after):
    """
    Retrieve results from the ARA API with pagination

    Args:
        client: ARA API client
        kind: Type of results to fetch ("playbooks", "hosts", "tasks")
        limit: Maximum number of results per page
        created_after: Timestamp to filter results
    """
    query = f"/api/v1/{kind}?order=-id&limit={limit}"
    if created_after is not None:
        query += f"&created_after={created_after}"

    try:
        response = client.get(query)
        items = response["results"]

        while response.get("next"):
            uri = response["next"].replace(client.endpoint, "")
            response = client.get(uri)
            items.extend(response["results"])

        return items
    except Exception as e:
        logging.error(f"Error fetching {kind}: {str(e)}")
        return []

# Core label sets with minimal but useful cardinality
PLAYBOOK_LABELS = [
    "name",     # For identifying specific playbooks
    "status"    # For filtering by outcome
]

TASK_LABELS = [
    "action",      # Module/action name (e.g., "command", "setup")
    "playbook_id"  # Link back to playbook
]

# Host result labels need both playbook_id and status
HOST_LABELS = [
    "playbook_id",  # Link back to playbook
    "status"       # Host result status (ok, failed, etc)
]

class AraPlaybookCollector:
    """Collects and exposes playbook-related metrics"""
    def __init__(self, client, log, limit):
        self.client = client
        self.log = log
        self.limit = limit

        self.metrics = {
            "count": Counter(
                "ara_playbook_count_total",
                "Total number of playbooks by status",
                ["status"]
            ),
            "duration": Histogram(
                "ara_playbook_runtime_seconds",
                "Duration of playbook runs",
                PLAYBOOK_LABELS,
                # 15m, 30m, 1h, 1.5h, 2h, 3h, 4h
                buckets=[900, 1800, 3600, 5400, 7200, 10800, 14400]
            ),
            "inventory": Gauge(
                "ara_playbook_inventory_size",
                "Number of hosts in playbook",
                ["playbook_id"]
            )
        }

    def collect_metrics(self, created_after=None):
        """Collect and update playbook metrics"""
        playbooks = get_search_results(self.client, "playbooks", self.limit, created_after)
        if not playbooks:
            return created_after

        latest_timestamp = None
        for playbook in playbooks:
            if latest_timestamp is None:
                timestamp = playbook["created"]
                # Handle timezone offset
                if '+' in timestamp:
                    timestamp = timestamp.split('+')[0] + 'Z'
                elif '-' in timestamp and timestamp.count('-') > 2:
                    timestamp = timestamp.rsplit('-', 1)[0] + 'Z'
                latest_timestamp = timestamp

            # Core labels
            labels = {
                "name": playbook.get("name", "unnamed"),
                "status": playbook.get("status", "unknown")
            }

            # Increment count by status
            self.metrics["count"].labels(status=playbook["status"]).inc()

            # Record duration if available
            if playbook["duration"] is not None:
                try:
                    seconds = cli_utils.parse_timedelta(playbook["duration"])
                    self.metrics["duration"].labels(**labels).observe(seconds)
                except ValueError:
                    self.log.warning(f"Invalid duration for playbook {playbook['id']}")

            # Record inventory size
            playbook_id = str(playbook["id"])
            if "items" in playbook and "hosts" in playbook["items"]:
                self.metrics["inventory"].labels(playbook_id=playbook_id).set(playbook["items"]["hosts"])

        return cli_utils.increment_timestamp(latest_timestamp) if latest_timestamp else created_after

class AraTaskCollector:
    """Collects and exposes task-related metrics"""
    def __init__(self, client, log, limit):
        self.client = client
        self.log = log
        self.limit = limit

        self.metrics = {
            "count": Counter(
                "ara_task_executions_total",
                "Number of task executions by action",
                TASK_LABELS
            ),
            "duration": Histogram(
                "ara_task_runtime_seconds",
                "Duration of task executions by action",
                TASK_LABELS,
                # 10s, 30s, 1m, 2m, 5m, 10m, 15m
                buckets=[10, 30, 60, 120, 300, 600, 900]
            ),
            "failures": Counter(
                "ara_task_failures_total",
                "Number of failed task executions by action",
                ["action", "playbook_id"]
            )
        }

    def collect_metrics(self, created_after=None):
        """Collect and update task metrics"""
        tasks = get_search_results(self.client, "tasks", self.limit, created_after)
        if not tasks:
            return created_after

        latest_timestamp = None
        for task in tasks:
            if latest_timestamp is None:
                timestamp = task["created"]
                if '+' in timestamp:
                    timestamp = timestamp.split('+')[0] + 'Z'
                elif '-' in timestamp and timestamp.count('-') > 2:
                    timestamp = timestamp.rsplit('-', 1)[0] + 'Z'
                latest_timestamp = timestamp

            # Core labels
            labels = {
                "action": task.get("action", "unknown"),
                "playbook_id": str(task.get("playbook", "unknown"))
            }

            # Update execution count
            self.metrics["count"].labels(**labels).inc()

            # Track failures separately
            if task.get("status") == "failed":
                self.metrics["failures"].labels(**labels).inc()

            # Record duration if available
            if task["duration"] is not None:
                try:
                    seconds = cli_utils.parse_timedelta(task["duration"])
                    self.metrics["duration"].labels(**labels).observe(seconds)
                except ValueError:
                    self.log.warning(f"Invalid duration for task {task['id']}")

        return cli_utils.increment_timestamp(latest_timestamp) if latest_timestamp else created_after

class AraHostCollector:
    """Collects and exposes host-related metrics"""
    def __init__(self, client, log, limit):
        self.client = client
        self.log = log
        self.limit = limit

        self.metrics = {
            # Core host metrics using a single metric for all states
            "results": Counter(
                "ara_host_result_total",
                "Host task results by type",
                HOST_LABELS + ["result"]
            )
        }

    def collect_metrics(self, created_after=None):
        """Collect and update host metrics"""
        hosts = get_search_results(self.client, "hosts", self.limit, created_after)
        if not hosts:
            return created_after

        latest_timestamp = None
        for host in hosts:
            if latest_timestamp is None:
                timestamp = host["created"]
                if '+' in timestamp:
                    timestamp = timestamp.split('+')[0] + 'Z'
                elif '-' in timestamp and timestamp.count('-') > 2:
                    timestamp = timestamp.rsplit('-', 1)[0] + 'Z'
                latest_timestamp = timestamp

            playbook_id = str(host.get("playbook", "unknown"))

            # Record results for each status type
            for result in ["ok", "failed", "changed", "skipped", "unreachable"]:
                if host.get(result, 0) > 0:
                    self.metrics["results"].labels(
                        playbook_id=playbook_id,
                        status=result,
                        result=result
                    ).inc(host[result])

        return cli_utils.increment_timestamp(latest_timestamp) if latest_timestamp else created_after

class PrometheusExporter(Command):
    """Exposes ARA metrics for Prometheus"""
    log = logging.getLogger(__name__)

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser = global_arguments(parser)

        parser.add_argument(
            '--playbook-limit',
            help='Max number of playbooks to request at once (default: 1000)',
            default=1000,
            type=int
        )
        parser.add_argument(
            '--task-limit',
            help='Max number of tasks to request at once (default: 2500)',
            default=2500,
            type=int
        )
        parser.add_argument(
            '--host-limit',
            help='Max number of hosts to request at once (default: 2500)',
            default=2500,
            type=int
        )
        parser.add_argument(
            '--poll-frequency',
            help='Seconds to wait until querying ara for new metrics (default: 60)',
            default=60,
            type=int
        )
        parser.add_argument(
            '--prometheus-port',
            help='Port on which the prometheus exporter will listen (default: 8001)',
            default=8001,
            type=int
        )
        parser.add_argument(
            '--max-days',
            help='Maximum number of days to backfill metrics for (default: 90)',
            default=90,
            type=int
        )
        return parser

    def take_action(self, args):
        if not HAS_PROMETHEUS_CLIENT:
            self.log.error("The prometheus_client python package must be installed to run this command")
            sys.exit(2)

        verify = False if args.insecure else True
        if args.ssl_ca:
            verify = args.ssl_ca

        client = get_client(
            client=args.client,
            endpoint=args.server,
            timeout=args.timeout,
            username=args.username,
            password=args.password,
            cert=args.ssl_cert,
            key=args.ssl_key,
            verify=verify,
            run_sql_migrations=False,
        )

        # Initialize collectors
        playbooks = AraPlaybookCollector(
            client=client,
            log=self.log,
            limit=args.playbook_limit
        )
        tasks = AraTaskCollector(
            client=client,
            log=self.log,
            limit=args.task_limit
        )
        hosts = AraHostCollector(
            client=client,
            log=self.log,
            limit=args.host_limit
        )

        # Start the HTTP server
        start_http_server(args.prometheus_port)
        self.log.info(f"ARA prometheus exporter listening on http://0.0.0.0:{args.prometheus_port}/metrics")

        # Calculate initial timestamp for backfilling
        created_after = (datetime.now() - timedelta(days=args.max_days)).isoformat()
        self.log.info(
            f"Backfilling metrics for the last {args.max_days} days since {created_after}..."
        )

        # Track latest timestamps for each collector
        latest = {
            "playbooks": created_after,
            "tasks": created_after,
            "hosts": created_after
        }

        # Main collection loop
        while True:
            try:
                latest["playbooks"] = playbooks.collect_metrics(latest["playbooks"])
                latest["tasks"] = tasks.collect_metrics(latest["tasks"])
                latest["hosts"] = hosts.collect_metrics(latest["hosts"])

                time.sleep(args.poll_frequency)
                self.log.info("Checking for updated metrics...")
            except Exception as e:
                self.log.error(f"Error collecting metrics: {str(e)}")
                time.sleep(args.poll_frequency)

