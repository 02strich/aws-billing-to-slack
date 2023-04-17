import datetime
import logging
import os
from collections import defaultdict
from typing import Dict, List, Optional

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# It seems that the sparkline symbols don't line up (probably based on font?) so put them last
# Also, leaving out the full block because Slack doesn't like it: '█'
sparks = ['▁', '▂', '▃', '▄', '▅', '▆', '▇']

def sparkline(datapoints):
    upper = max(datapoints)
    n_sparks = len(sparks) - 1

    line = ""
    for dp in datapoints:
        scaled = 1 if upper == 0 else dp/upper
        which_spark = round(scaled * n_sparks)
        line += (sparks[which_spark])

    return line


def delta(costs):
    if (len(costs) > 1 and costs[-1] >= 1 and costs[-2] >= 1):
        # This only handles positive numbers
        result = ((costs[-1]/costs[-2])-1)*100.0
    else:
        result = 0
    return result


def find_by_key(values: list, key: str, value: str):
    for item in values:
        if item.get(key) == value:
            return item
    return None


def lambda_handler(event, context):
    length = int(os.environ.get("LENGTH", "15"))
    cost_aggregation = os.environ.get("COST_AGGREGATION", "UnblendedCost")

    buffer = report_cost(max_entries=length, cost_aggregation=cost_aggregation)

    slack_hook_url = os.environ.get('SLACK_WEBHOOK_URL')
    if slack_hook_url:
        publish_slack(slack_hook_url, buffer)


def report_cost(max_entries: int, cost_aggregation: str = "UnblendedCost"):
    today = datetime.datetime.today()
    a_week_ago = today - datetime.timedelta(days=7)
    two_months_ago = today - datetime.timedelta(days=70)

    client = boto3.client('ce')
    monthly_cost_and_usage_data = client.get_cost_and_usage(
        TimePeriod={
            "Start": two_months_ago.strftime('%Y-%m-%d'),
            "End": today.strftime('%Y-%m-%d'),
        },
        Granularity="MONTHLY",
        Filter={
            "Not": {
                "Dimensions": {
                    "Key": "RECORD_TYPE",
                    "Values": [
                        "Credit",
                        "Refund",
                        "Upfront",
                        "Support",
                    ]
                }
            }
        },
        Metrics=[cost_aggregation],
        GroupBy=[
            {
                "Type": "DIMENSION",
                "Key": "LINKED_ACCOUNT",
            },
        ],
    )
    daily_cost_and_usage_data = client.get_cost_and_usage(
        TimePeriod={
            "Start": a_week_ago.strftime('%Y-%m-%d'),
            "End": today.strftime('%Y-%m-%d'),
        },
        Granularity="DAILY",
        Filter={
            "Not": {
                "Dimensions": {
                    "Key": "RECORD_TYPE",
                    "Values": [
                        "Credit",
                        "Refund",
                        "Upfront",
                        "Support",
                    ]
                }
            }
        },
        Metrics=[cost_aggregation],
        GroupBy=[
            {
                "Type": "DIMENSION",
                "Key": "LINKED_ACCOUNT",
            },
        ],
    )

    # first create a dict of dicts
    # then loop over the services and loop over the list_of_dates
    # and this means even for sparse data we get a full list of costs
    cost_per_month_dict: Dict[str, List[float]] = defaultdict(list)
    month_count = 0
    for month in reversed(monthly_cost_and_usage_data['ResultsByTime']):
        if month_count > 1:
           continue ## only process two arounds, as prior data will be partial

        for group in month['Groups']:
            key = group['Keys'][0]

            dimension = find_by_key(monthly_cost_and_usage_data["DimensionValueAttributes"], "Value", key)
            if dimension:
                key += " ("+dimension["Attributes"]["description"]+")"

            cost = float(group['Metrics'][cost_aggregation]['Amount'])
            cost_per_month_dict[key].append(cost)
        month_count += 1

    cost_per_day_dict: Dict[str, List[float]] = defaultdict(list)
    for month in reversed(daily_cost_and_usage_data['ResultsByTime']):
        for group in month['Groups']:
            key = group['Keys'][0]

            dimension = find_by_key(daily_cost_and_usage_data["DimensionValueAttributes"], "Value", key)
            if dimension:
                key += " ("+dimension["Attributes"]["description"]+")"

            cost = float(group['Metrics'][cost_aggregation]['Amount'])
            cost_per_day_dict[key].append(cost)
        month_count += 1

    # Sort the map by yesterday's cost
    most_expensive = sorted(cost_per_month_dict.items(), key=lambda i: i[1][0], reverse=True)
    longest_name_len = len(max(cost_per_month_dict.keys(), key = len))

    buffer = f"{'AWS Accounts':^{longest_name_len}} | Month-to-date | yesterday | {'Last month':>5}\n"
    for name, costs in most_expensive[:max_entries]:
        buffer += f"{name:{longest_name_len}} | {costs[0]:>12,.2f}$ | {cost_per_day_dict.get(name, [0])[0]:>8,.2f}$ | {costs[1]:.2f}$\n"

    other_costs = [0.0] * 2
    for _, costs in most_expensive[max_entries:]:
        for i, cost in enumerate(costs):
            other_costs[i] += cost
    buffer += f"{'Other':{longest_name_len}} | {other_costs[0]:>12,.2f}$ |           | {other_costs[1]:.2f}$\n"

    return buffer


def publish_slack(hook_url, buffer):
    resp = requests.post(
        hook_url,
        json={
            "text": "```\n" + buffer + "\n```",
        }
    )

    if resp.status_code != 200:
        logger.warning("HTTP %s: %s" % (resp.status_code, resp.text))
