import argparse
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import chain
from typing import Dict, List, Iterator
from datadog_api_client.v1.model.point import Point
from datadog_api_client.v1.model.series import Series
from enum import Enum

from common.admins import ADMINS
from common.datadog_api import DatadogApi
from common.git_hub_api import substitute
from common.google_api import get_spreadsheets
from common.issue import get_issues, Issue, get_delta_days, get_date_time
from common.repos import ALL_REPOS, get_repos

GOOGLE_SHEET_ID = '1cQOOT5aYxfXOSwEV0cJyf01KkV-uKCBJnKK3PHjouCE'
GOOGLE_SHEET_NAME_DAILY = 'Daily'
GOOGLE_SHEET_NAME_WEEKLY = 'Weekly'
DAILY = 'daily'
WEEKLY = 'weekly'
TWILIO = 'twilio'
SENDGRID = 'sendgrid'
STALE_DAYS = 365
# Tuple to specify the metric name and type to be collected in Datadog
# Type could be 'count', 'mean', 'median', 'min' or 'max'
DATADOG_METRICS = [('issue_count', 'count'), ('time_to_contact', 'mean'), ('time_to_contact_pr', 'mean'),
                   ('time_to_close', 'mean')]


def base_type():
    return {'nodes': defaultdict(base_type), 'metrics': {}}


class DatadogSeriesType(str, Enum):
    GAUGE = 'gauge'
    COUNT = 'count'


class MetricCollector:

    def __init__(self):
        self.metrics = base_type()
        self.untagged_issues = []
        self.all_metrics = []
        self.datadog_api = DatadogApi()

        # Load up the spreadsheet connector early to validate credentials.
        self.spreadsheets = get_spreadsheets()

    def run(self, run_options: dict) -> None:
        repos = run_options['repos']
        start_date = run_options['start_date']
        end_date = run_options['end_date']
        reporting_period = run_options['reporting_period'] or end_date

        print(f'repos to run the metrics on: {repos}')
        global_node = self.metrics

        for repo in repos:
            org_node = global_node['nodes'][repo.org]
            repo_node = org_node['nodes'][repo.name]

            self.process_repo(repo_node, repo.org, repo.name, start_date, end_date)

        # If we have any untagged issues, print them
        if self.untagged_issues:
            print('These issues need a "type" label:')
            for issue in self.untagged_issues:
                print(issue.url)

        for org in global_node['nodes']:
            org_node = global_node['nodes'][org]
            for repo in org_node['nodes']:
                repo_node = org_node['nodes'][repo]
                self.aggregate(repo_node)
                name = f'https://github.com/{org}/{repo}'
                self.summarize(name, reporting_period, repo_node)

        # Until we move our entire data to Datadog, we will continue to push data to Google Sheet as well
        self.output_google_sheet(reporting_period=reporting_period)

        # Convert data to Datadog time series
        datadog_series = []

        for org in global_node['nodes']:
            org_node = global_node['nodes'][org]
            for repo in org_node['nodes']:
                repo_node = org_node['nodes'][repo]
                datadog_series += self.get_series_for_datadog(repo_node, org, repo)

        # Helps with debugging in Github Actions logs
        print("Datadog series data:", datadog_series)

        # Submit data to Datadog
        self.datadog_api.submit_metrics(datadog_series)

    def get_series_for_datadog(self, repo_node: Dict, org: str, repo: str) -> Iterator[Series]:
        for metric_name, metric_type in DATADOG_METRICS:

            try:
                data = repo_node['metrics'][metric_name][metric_type]

            except KeyError as e:
                print(f'Failed to find metric "{metric_name}" for {repo}: {e}')
                continue

            yield Series(
                metric=f'library.{metric_name}.{metric_type}',
                type=f'{DatadogSeriesType.GAUGE}',
                points=[Point([datetime.now().timestamp(), float(data)])],
                tags=[f'org:{org}', f'repo:{org}/{repo}', 'type:helper'],
            )

    def process_repo(self, nodes: Dict,
                     org: str, repo: str,
                     start_date: str, end_date: str) -> None:
        start_date = get_date_time(start_date)
        end_date = get_date_time(end_date)
        stale_date = datetime.strptime(datetime.now().strftime(DATE_TIME_FORMAT), DATE_TIME_FORMAT) - timedelta(
            days=STALE_DAYS)
        stale_date = get_date_time(stale_date.strftime(DATE_TIME_FORMAT))
        issues = get_repo_issues(org, repo)
        issue_count = 0
        pr_count = 0

        for issue_json in issues:
            issue = Issue(issue_json, end_date=end_date)

            if issue.author in ADMINS:
                continue

            if issue.created_at > end_date:
                continue

            if issue.created_at < stale_date:
                continue

            issue.process_events()

            if issue.created_at >= start_date:
                issue_category = issue.get_issue_category()

                self.add_time_to_resolve(issue)

                if 'time_to_close' in issue.metrics:
                    time_to_close = issue.metrics.pop('time_to_close')

                    if issue.get_issue_status() not in ['duplicate', 'invalid']:
                        if issue.first_admin_comment:
                            if issue_category:
                                issue.metrics[f'time_to_close_{issue_category}'] = time_to_close
                            else:
                                self.untagged_issues.append(issue)

                if 'time_awaiting_resolution' in issue.metrics:
                    resolution = issue.metrics.pop('time_awaiting_resolution')

                    if issue_category:
                        issue.metrics[f'time_awaiting_resolution_{issue_category}'] = resolution
                    else:
                        self.untagged_issues.append(issue)

                if not issue.first_admin_comment:
                    issue.metrics.pop('time_to_close_pr', None)

            nodes['nodes'][issue.url]['metrics'] = issue.metrics

            if issue.events != []:
                if issue.events is not None:
                    last_update = issue.events[-1].get('createdAt', None)
                if last_update is None:
                    last_update = issue.events[-1]['commit']['committedDate']
            else:
                last_update = issue.created_at

            if not issue.closed and last_update > stale_date:
                time_open = get_delta_days(issue.created_at, end_date)
                if '/pull/' in issue.url:
                    pr_count += 1
                    nodes['nodes'][issue.url]['metrics']['pr_count'] = pr_count
                    nodes['nodes'][issue.url]['metrics']['time_open_pr'] = time_open
                else:
                    issue_count += 1
                    nodes['nodes'][issue.url]['metrics']['issue_count'] = issue_count
                    nodes['nodes'][issue.url]['metrics']['time_open_issue'] = time_open

    def add_time_to_resolve(self, issue: Issue) -> None:
        for ext in {'', '_pr'}:
            if f'time_to_close{ext}' in issue.metrics and issue.first_admin_comment:
                contact = issue.metrics[f'time_to_contact{ext}']
                respond = issue.metrics.get(f'time_to_respond{ext}', [])
                close = issue.metrics[f'time_to_close{ext}']

                issue.metrics[f'time_to_resolve'] = sum(contact) + sum(respond) + sum(close)

    def aggregate(self, node: Dict) -> None:
        metrics: Dict[str, List[float]] = {}

        for item in node['nodes'].values():
            for metric_id, value in item['metrics'].items():
                if metric_id not in metrics:
                    metrics[metric_id] = []

                if isinstance(value, dict):
                    value = value['values']

                if not isinstance(value, list):
                    value = [value]

                metrics[metric_id].extend(value)

        for metric_id, values in metrics.items():
            node['metrics'][metric_id] = {
                'values': sorted(values),
                'count': len(values),
                'min': min(values),
                'max': max(values),
                'mean': statistics.mean(values),
                'median': statistics.median(values),
            }

    def summarize(self, name: str, reporting_date: str, node: Dict) -> None:
        repo_metrics = {'name': name, 'date': datetime.now().strftime(DATE_TIME_FORMAT)}

        metrics = node['metrics']

        for metric, values in metrics.items():
            for k, v in values.items():
                if k != 'values':
                    repo_metrics[f'{metric}_{k}'] = v

        self.all_metrics.append(repo_metrics)

    def output_google_sheet(self, reporting_period: str) -> None:
        if reporting_period == DAILY:
            google_sheet_name = GOOGLE_SHEET_NAME_DAILY
        if reporting_period == WEEKLY:
            google_sheet_name = GOOGLE_SHEET_NAME_WEEKLY

        response = self.spreadsheets.values().get(spreadsheetId=GOOGLE_SHEET_ID,
                                                  range=f'{google_sheet_name}!1:1').execute()
        header = response.get('values', [[]])[0]

        values = []

        for metrics in self.all_metrics:
            row = []
            values.append(row)

            for metric_id in header:
                row.append(metrics.get(metric_id))

            for metric_id, value in metrics.items():
                if metric_id not in header:
                    header.append(metric_id)
                    row.append(value)

        self.spreadsheets.values().update(spreadsheetId=GOOGLE_SHEET_ID,
                                          range=f'{google_sheet_name}!1:1',
                                          valueInputOption='USER_ENTERED',
                                          body={'values': [header]}).execute()

        self.spreadsheets.values().append(spreadsheetId=GOOGLE_SHEET_ID,
                                          range=f'{google_sheet_name}!A2:A',
                                          valueInputOption='USER_ENTERED',
                                          body={'values': values}).execute()


@lru_cache(maxsize=None)
def get_repo_issues(org: str, repo: str):
    fragment_template = """
... on %issue_type% {
    author {
        login
    }
    createdAt
    url
    timelineItems(first: 100, itemTypes: [LABELED_EVENT UNLABELED_EVENT
                                          ISSUE_COMMENT
                                          CLOSED_EVENT REOPENED_EVENT
                                          %additional_event_names%]) {
        nodes {
            __typename
            ... on LabeledEvent {
                createdAt
                label {
                    id
                    name
                }
            }
            ... on UnlabeledEvent {
                createdAt
                label {
                    id
                    name
                }
            }
            ... on IssueComment {
                createdAt
                author {
                    login
                }
            }
            ... on ClosedEvent {
                createdAt
            }
            ... on ReopenedEvent {
                createdAt
            }
            %additional_event_types%
        }
    }
}"""

    fragment_params = [
        {
            'issue_type': 'Issue'
        },
        {
            'issue_type': 'PullRequest',
            'additional_event_names': ['PULL_REQUEST_COMMIT', 'PULL_REQUEST_REVIEW',
                                       'MERGED_EVENT'],
            'additional_event_types': [
                '... on PullRequestCommit {commit {committedDate author {user {login}} status {state} statusCheckRollup {state}}}',
                '... on PullRequestReview {createdAt state author {login}}',
                '... on MergedEvent {createdAt}']
        }
    ]

    inline_fragments = [substitute(fragment_template, fragment)
                        for fragment in fragment_params]

    return list(get_issues(org, repo, ''.join(inline_fragments)))


DATE_TIME_FORMAT = '%Y-%m-%d'


def get_date_range(start_date: str, end_date: str) -> List[str]:
    start_date = datetime.strptime(start_date, DATE_TIME_FORMAT)
    end_date = datetime.strptime(end_date, DATE_TIME_FORMAT)

    while start_date <= end_date:
        yield start_date.strftime(DATE_TIME_FORMAT)
        start_date = start_date + timedelta(days=7)


def run_backfill() -> None:
    mondays = get_date_range('2020-01-06', '2020-05-18')
    fridays = get_date_range('2020-05-22', datetime.now().strftime(DATE_TIME_FORMAT))

    for end_date in chain(mondays, fridays):
        options = {
            'repos': get_repos(),
            'start_date': '2020-01-01',
            'end_date': end_date,
        }
        MetricCollector().run(options)


def run_now(reporting_period: str, org: List[str], include: List[str], exclude: List[str]) -> None:
    today = datetime.now().strftime(DATE_TIME_FORMAT)
    repos = get_repos(org, include, exclude)
    options = {
        'repos': repos,
        'start_date': today,
        'end_date': today,
        'reporting_period': reporting_period,
    }

    if reporting_period == DAILY:
        MetricCollector().run(options)
    if reporting_period == WEEKLY:
        start_date = datetime.strptime(today, DATE_TIME_FORMAT) - timedelta(days=7)
        start_date = start_date.strftime(DATE_TIME_FORMAT)
        options['start_date'] = start_date
        MetricCollector().run(options)


def parse_args():
    parser = argparse.ArgumentParser(description='dx-automator')
    parser.add_argument('--period', '-p', required=True, help='period to run the metrics on',
                        choices=[DAILY, WEEKLY])
    parser.add_argument('--org', '-o', nargs='*',
                        help='if none specified, runs on all orgs',
                        default=[],
                        choices=ALL_REPOS.keys())
    parser.add_argument('--include', '-i', nargs='*',
                        help='repos to include',
                        default=[],
                        choices=[repo.name for repo in get_repos()])
    parser.add_argument('--exclude', '-e', nargs='*',
                        help='repos to exclude',
                        default=[],
                        choices=[repo.name for repo in get_repos()])

    parsed_args = vars(parser.parse_args())

    if not parsed_args:
        parser.print_help()
        sys.exit(1)

    return parsed_args


if __name__ == '__main__':
    args = parse_args()

    run_now(args['period'], args['org'], args['include'], args['exclude'])
