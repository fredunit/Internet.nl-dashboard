from datetime import timedelta
from typing import List

from constance import config
from django.utils import timezone

from dashboard.internet_nl_dashboard.models import (Account, AccountInternetNLScan,
                                                    AccountInternetNLScanLog, UrlListReport)


def get_scan_monitor_data(account: Account) -> List:

    # at least .defer('report__calculation'), as that takes a lot of time to load in(!)
    scans = AccountInternetNLScan.objects.all().filter(
        account=account,
        urllist__is_deleted=False
    ).order_by('-pk')[0:30].select_related(
        'urllist', 'scan', 'report'
    ).defer('report__calculation')

    """
    using defer is about as fast, and better to program with

    .only('state', 'id', 'report__id', 'scan__finished_on', 'scan__started_on', 'scan__type', 'scan__finished',
       'scan__status_url', 'scan__friendly_message', 'scan__success', 'urllist__name', 'urllist_id',
       'scan__last_check', )
    """

    # prevent a bunch of query-per-access:
    internet_nl_api_url = config.INTERNET_NL_API_URL

    response = []
    for scan in scans:
        # nested query, should be optimized, prefetch doesn't work here as it's one line down... for 30 queries
        # it's probably fine but far from ideal.

        last_report_id = None
        # Finished means also report created, mail sent, etc.
        if scan.state == "finished":
            # first report within the next day
            if scan.report:
                last_report_id = scan.report.id
            else:
                # heuristic approach, this approach was used before the connection between report and scan
                # was solidified (and became reliable). This code is here to be backwards compatible with
                # existing reports.
                last_report = UrlListReport.objects.all().filter(
                    urllist=scan.urllist,
                    at_when__lte=scan.finished_on + timedelta(hours=24),
                    at_when__gte=scan.finished_on
                ).order_by('-id').only('id').first()

                if last_report:
                    last_report_id = last_report.id

        if scan.state == "finished":
            runtime = scan.finished_on - scan.started_on
        else:
            runtime = timezone.now() - scan.started_on

        # get complete log from this scan.
        logs = AccountInternetNLScanLog.objects.all().filter(scan=scan).order_by('-at_when')
        log_messages = []
        for log in logs:
            log_messages.append({'at_when': log.at_when, 'state': log.state})

        runtime = runtime.total_seconds() * 1000

        if not scan.scan:
            response.append({
                'id': scan.id,
                # mask that there is a mail_dashboard variant.
                'type': "web" if scan.urllist.scan_type == "web" else "mail",
                'started': True,
                'started_on': scan.started_on,
                'finished': scan.finished,
                'finished_on': scan.finished_on,
                'status_url': f"{internet_nl_api_url}/requests/{scan.scan.scan_id}",
                'message': scan.state,
                'success': scan.finished,
                'list': scan.urllist.name,
                'list_id': scan.urllist.id,
                'last_check': scan.scan.last_state_check,
                'runtime': runtime,
                'last_report_id': last_report_id,
                'state': scan.state,
                'log': log_messages
            })

        else:
            response.append({
                'id': scan.id,
                # mask that there is a mail_dashboard variant.
                'type': "web" if scan.scan.type == "web" else "mail",
                'started': True,
                'started_on': scan.started_on,
                'finished': scan.finished,
                'finished_on': scan.finished_on,
                'status_url': f"{internet_nl_api_url}/requests/{scan.scan.scan_id}",
                'message': scan.state,
                'success': scan.finished,
                'list': scan.urllist.name,
                'list_id': scan.urllist.id,
                'last_check': scan.scan.last_state_check,
                'runtime': runtime,
                'last_report_id': last_report_id,
                'state': scan.state,
                'log': log_messages
            })

    return response
