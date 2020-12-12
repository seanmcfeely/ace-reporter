#!/usr/bin/env python3

import os
import sys
import glob
import json
import logging
import pymysql
import argparse
import coloredlogs
import configparser
import traceback
import pandas_bokeh

import smtplib
import email
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from jinja2 import Template
from tabulate import tabulate
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from ace_metrics.alerts import ( get_alerts_between_dates,
                             VALID_ALERT_STATS, 
                             FRIENDLY_STAT_NAME_MAP,
                             statistics_by_month_by_dispo,
                             generate_hours_of_operation_summary_table,
                             generate_overall_summary_table,
                             define_business_time
                            )

from ace_metrics.alerts.users import alert_quantities_by_user_by_month

from ace_metrics.alerts.alert_types import ( unique_alert_types_between_dates,
                                             count_quantites_by_alert_type,
                                             get_alerts_between_dates_by_type,
                                             generate_alert_type_stats,
                                             all_alert_types,
                                             alert_type_quantities_by_category_by_month
                                            )

from ace_metrics.events import ( get_events_between_dates,
                             get_incidents_from_events,
                             add_email_alert_counts_per_event,
                             count_event_dispositions_by_time_period,
                             EVENT_COUNT_TIME_DB_QUERY,
                             EVENT_DISPOSITIONS
                            )

from ace_metrics.helpers import generate_html_plot, dataframes_to_xlsx_bytes

#from helpers import send_email_notification

# configure logging #
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - [%(levelname)s] %(message)s')

# we are local
os.environ['no_proxy'] = '.local'

logger = logging.getLogger()
coloredlogs.install(level='INFO', logger=logger)

# globals
HOME_PATH = os.path.dirname(os.path.abspath(__file__))
INCOMING_DIR_NAME = 'incoming'
INCOMING_DIR = os.path.join(HOME_PATH, INCOMING_DIR_NAME)
ARCHIVE_DIR_NAME = 'archive'
ARCHIVE_DIR = os.path.join(HOME_PATH, ARCHIVE_DIR_NAME)

# requirements
REQURIED_DIRS = [INCOMING_DIR_NAME, ARCHIVE_DIR_NAME, 'logs', 'var']
for path in [os.path.join(HOME_PATH, x) for x in REQURIED_DIRS]:
    if not os.path.isdir(path):
        try:
            os.mkdir(path)
        except Exception as e:
            sys.stderr.write("ERROR: cannot create directory {0}: {1}\n".format(
                path, str(e)))
            sys.exit(1)

# helper functions
def send_email_notification(smtp_config: configparser.SectionProxy,
                            report_template_path: str,
                            subject: str,
                            recipients: list,
                            report_paths: list,
                            report_context_map: dict={}):
    """Sends an email notification to a user."""

    # is SMTP enabled?
    if not smtp_config.getboolean('enabled'):
        logging.debug("smtp is not enabled. Aborting email notification")
        return False

    context = tabulate(report_context_map, headers="keys", tablefmt="simple")
    with open(f'{report_template_path}.txt', 'r') as fp:
        text_content = fp.read().replace('{<[report_context]>}', context)

    template = Template(open(f'{report_template_path}.html').read())
    html_content = template.render(report_context_map=report_context_map)

    # build email
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = smtp_config["mail_from"]
    message["Reply-To"] = smtp_config["reply_to"]
    message["To"] = f"{', '.join(recipients)}"
    message['CC'] = smtp_config['cc_list']

    message.attach(MIMEText(text_content, "plain"))
    message.attach(MIMEText(html_content, "html"))

    for report_path in report_paths:
        filename = report_path[report_path.rfind('/')+1:report_path.rfind('.')]
        part = MIMEBase("application", "octet-stream")
        with open(report_path, 'rb') as attachment:
            part.set_payload(attachment.read())
        
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename= {filename}",
        )
        message.attach(part)

    with smtplib.SMTP(smtp_config['server']) as smtp_server:
        #smtp_server.set_debuglevel(2) # will show the raw email transaction
        logging.info(f"sending email notification to {recipients} with subject {message['Subject']}")
        smtp_server.send_message(message)

    return True

def write_error_report(message):
    """Record unexpected errors."""
    logging.error(message)
    traceback.print_exc()

    try:
        output_dir = 'error_reporting'
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        with open(os.path.join(output_dir, datetime.now().strftime('%Y-%m-%d:%H:%M:%S.%f')), 'w') as fp:
            fp.write(message)
            fp.write('\n\n')
            fp.write(traceback.format_exc())

    except Exception as e:
        traceback.print_exc()

def floor_datetime_month(dt):
    """Floor a datetime to the absolutle begining of the month."""
    return dt - timedelta(days=dt.day-1, hours=dt.hour, minutes=dt.minute, seconds=dt.second)

def floor_datetime_week(dt):
    """Floor a datetime to the absolute beginning of the most recent week."""
    return dt - timedelta(days=dt.isoweekday() % 7, hours=dt.hour, minutes=dt.minute, seconds=dt.second)


def email_reports_based_on_configuration(config: configparser.ConfigParser, report_context_map: dict, approved_reports: list, archive=False):
    # Send configured email notifications
    notification_list = [section for section in config.sections() if section.startswith('report_type_')]
    for notification_key in notification_list:
        report_type = notification_key[len('report_type_'):]
        if report_type not in approved_reports:
            logging.info(f"skipping {notification_key} as {report_type} not in approved report list.")
            continue
        notification_config = config[notification_key]
        notification_template_path = os.path.join(HOME_PATH, notification_config['email_template'])
        recipients = []
        for report_recipient_group in notification_config['recipient_groups'].split(','):
            recipients.extend(config['recipient_groups'][report_recipient_group].split(','))

        report_paths = glob.glob(f"{os.path.join(INCOMING_DIR)}/*.{report_type}")
        if archive:
            report_paths = [rp.replace(INCOMING_DIR_NAME, ARCHIVE_DIR_NAME) for rp in report_paths]
        send_email_notification(config['smtp'],
                                notification_template_path,
                                subject=notification_config['subject'].format(datetime.now().date()),
                                recipients=recipients,
                                report_context_map=report_context_map[report_type],
                                #context=tabulate(report_context_map[report_type], headers="keys", tablefmt="html"),
                                report_paths=report_paths)

def save_report_context_map(report_context_map):
    """Save a copy of the report context map.
    
       Allows for existing reports to be sent without 
       totally rebuilding them.
    """
    report_context_map_path = os.path.join(HOME_PATH, 'var', 'report_context_map')
    try:
        with open(report_context_map_path, 'w') as fp:
            json.dump(report_context_map, fp)
        return True
    except Exception as e:
        logging.error(f"Problem writing {report_context_map_path}: {e}")
    return False

def load_report_context_map():
    """Load the previous report_context_map."""
    report_context_map_path = os.path.join(HOME_PATH, 'var', 'report_context_map')
    if not os.path.exists(report_context_map_path):
        logging.debug(f"{report_context_map_path} does not exist")
        return {}
    with open(report_context_map_path, 'r') as fp:
            report_context_map = json.load(fp)

    return report_context_map


def main():

    parser = argparse.ArgumentParser(description="Sends ACE Metric reports to users based on role/need map.")
    parser.add_argument('-d', '--debug', action='store_true', help="Turn on debug logging.")
    parser.add_argument('--logging-config', required=False, default='etc/logging.ini', dest='logging_config',
        help="Path to logging configuration file.  Defaults to etc/logging.ini")
    parser.add_argument('-c', '--config', required=False, default='etc/config.ini', dest='config_path',
        help="Path to configuration file.  Defaults to etc/config.ini")

    parser.add_argument('-s', '--start_datetime', action='store', default=None,
                        help="Override the start datetime data is in scope. Format: YYYY-MM-DD HH:MM:SS.")
    parser.add_argument('-e', '--end_datetime', action='store', default=None,
                         help="Override the end datetime data is in scope. Format: YYYY-MM-DD HH:MM:SS.")

    # hard code for now
    available_reports = {'high_level': "IDR operational alert, event, and indicator based metrics."}
    parser.add_argument('-r', '--report-type', action='append', default=list(available_reports.keys()), 
                        choices=list(available_reports.keys()), dest="approved_reports",
                        help="Specify specific reports to generate. Default: All configured reports.")

    parser.add_argument('-sar', '--send-archived-report', action='append', default=[], 
                        choices=list(available_reports.keys()), dest="send_archived_reports",
                        help="Specify specific reports to send from archive. Default: All archived reports.")

    args = parser.parse_args()

    # work out of home dir
    os.chdir(HOME_PATH)

    coloredlogs.install(level='INFO', logger=logging.getLogger())

    if args.debug:
        coloredlogs.install(level='DEBUG', logger=logging.getLogger())

    config = configparser.ConfigParser()
    config.read(args.config_path)

    # keep track of the reports
    # load and save the report so it stays up-to-date with all archived report types
    report_context_map = load_report_context_map()

    if args.send_archived_reports:
        report_context_map = load_report_context_map()
        return email_reports_based_on_configuration(config, report_context_map, args.send_archived_reports, archive=True)
        
    smtp_config = config['smtp']
    db_config = config['database']
    #recipient_groups = config['recipient_groups']

    # default selected comanies
    companies = config['global'].get('companies', "").split(',')

    # connect to ACE DB
    ssl_settings = None
    if os.path.exists(db_config.get('ssl_ca_path')):
        ssl_settings = {'ca': db_config['ssl_ca_path']}

    password = db_config.get('pass')
    if not password:
        password = getpass(f"Enter password for {db_config['host']}: ")

    db = pymysql.connect(host=db_config['host'], user=db_config['user'], password=password, database=db_config['database'], ssl=ssl_settings)

    # set date scope time periods
    start_date = end_date = event_start_date = event_end_date = None
    if args.start_datetime:
        start_date = datetime.strptime(args.start_datetime, '%Y-%m-%d %H:%M:%S')
        event_start_date = start_date # eh
    if args.end_datetime:
        end_date = datetime.strptime(args.end_datetime, '%Y-%m-%d %H:%M:%S')
    else:
        end_date = event_end_date = datetime.utcnow()

    #######################
    ## High Level Report ##
    #######################
    # alert stat tables
    report_type = "high_level"
    report_file_name = f"IDR High Level Report - {datetime.now().date()}.html"
    report_config = config[f"report_type_{report_type}"]
    report_template = report_config['report_template']
    if report_config.getboolean('exact_end_time_period'):
        end_date = floor_datetime_month(end_date)
        event_end_date = floor_datetime_week(event_end_date)
    if start_date is None:
        start_date = end_date - relativedelta(months=report_config.getint('alert_data_scope_months_before_end_time'))
        event_start_date = event_end_date - relativedelta(months=report_config.getint('event_data_scope_months_before_end_time'))
    report_context_map[report_type] = {}
    report_context_map[report_type][report_file_name] = []
    tables_for_xlsx = []

    # general alert statistic plots & tables
    alerts = get_alerts_between_dates(start_date, end_date, db, selected_companies=companies)  
    alert_stat_map = statistics_by_month_by_dispo(alerts)
    alert_stat_report_map = {}
    for stat in VALID_ALERT_STATS:
        alert_stat_map[stat].name = FRIENDLY_STAT_NAME_MAP[stat]
        alert_stat_report_map[stat] = {'table': alert_stat_map[stat],
                                       'html_plot': generate_html_plot(alert_stat_map[stat])}
        report_context_map[report_type][report_file_name].append(f"Alerts: {FRIENDLY_STAT_NAME_MAP[stat]}")
        tables_for_xlsx.append(alert_stat_map[stat])

    # append the quantity by analyst plot & table
    user_dispositions_per_month = alert_quantities_by_user_by_month(start_date, end_date, db)
    alert_stat_report_map['analyst-alert-quantities'] = {'table': user_dispositions_per_month,
                                                         'html_plot': generate_html_plot(user_dispositions_per_month)}
    report_context_map[report_type][report_file_name].append(f"Alerts: {user_dispositions_per_month.name}")
    tables_for_xlsx.append(user_dispositions_per_month)

    # alerts by alert type quantities plot & table
    alert_type_categories_key = {}
    for k,v in config['alert_type_categories_key'].items():
        alert_type_categories_key[k] = v.split(',')
    alert_category_quantities = alert_type_quantities_by_category_by_month(start_date, end_date, db, alert_type_categories_key)
    alert_category_quantities.description = ('Alert types can vary on an individual hunt '
        'and tool basis. For this reason, categories (or buckets) contain alerts that are '
        'based on similar data sources or tools. This metric displays how Alert Types change over time.')
    alert_stat_report_map['alert-type-quantities'] = {'table': alert_category_quantities,
                                                       'html_plot': generate_html_plot(alert_category_quantities)}
    report_context_map[report_type][report_file_name].append(f"Alerts: {alert_category_quantities.name}")
    tables_for_xlsx.append(alert_category_quantities)

    # operational alert tables without plots
    independent_alert_tables = {}
    business_hours = define_business_time()
    hop_df = generate_hours_of_operation_summary_table(alerts.copy(), business_hours)
    independent_alert_tables["hop"] = hop_df
    report_context_map[report_type][report_file_name].append(f"Alerts: {hop_df.name}")
    tables_for_xlsx.append(hop_df)
    sla_df = generate_overall_summary_table(alerts.copy(), business_hours)
    independent_alert_tables["sla"] = sla_df
    report_context_map[report_type][report_file_name].append(f"Alerts: {sla_df.name}")
    tables_for_xlsx.append(sla_df)

    ## Begin Event Section ##
    event_stat_report_map = {}

    # event quantities by disposition
    time_period = report_config.get('event_time_period', 'Week')
    year_week_format = '%%Y%%U'
    year_month_format = '%%Y%%m'
    time_format = year_week_format
    if time_period == 'Month':
        time_format = year_month_format
    elif time_period != 'Week':
        logging.error(f"unrecognized time period: {time_period}")

    event_query = EVENT_COUNT_TIME_DB_QUERY.replace('{<[TIME_FORMAT]>}', time_format).replace('{<[TIME_KEY]>}', time_period)
    events = get_events_between_dates(event_start_date, event_end_date, db, 
                                      selected_companies=companies,
                                      event_query=event_query)
    events.set_index(time_period, inplace=True)
    events_by_time_period_by_dispo = count_event_dispositions_by_time_period(time_period, events, EVENT_DISPOSITIONS)
    events_by_time_period_by_dispo.name = f"Event/Incident Quantities per {time_period}"
    _plot = generate_html_plot(events_by_time_period_by_dispo, xlabel=time_period, ylabel="Quantity")
    event_stat_report_map['event-quantities'] = {'table': events_by_time_period_by_dispo,
                                                 'html_plot': _plot}
    report_context_map[report_type][report_file_name].append(f"Events: {events_by_time_period_by_dispo.name}")
    tables_for_xlsx.append(events_by_time_period_by_dispo)

    # event tables for xlsx
    # NOTE: update email template to comment on attached xlsx documents
    event_tables = {}
    # follow config to scope down the data as these tables can be huge
    raw_event_data_start_date = event_end_date - relativedelta(months=report_config.getint('raw_event_incident_table_data_scope_months'))
    events = get_events_between_dates(raw_event_data_start_date, event_end_date, db, selected_companies=companies)
    add_email_alert_counts_per_event(events, db)
    incidents = get_incidents_from_events(events)
    tables_for_xlsx.append(incidents)
    events.drop(columns=['id'], inplace=True)
    tables_for_xlsx.append(events)

    # render the html report
    template = None
    with open(os.path.join(HOME_PATH, report_template), 'r') as fp:
        template = Template(fp.read())
    if template is None:
        logging.error(f"failed to load templated.")
        return False
    report_html = template.render(alert_stat_report_map=alert_stat_report_map,
                                  event_stat_report_map=event_stat_report_map,
                                  independent_alert_tables=independent_alert_tables,
                                  event_tables={}, # removed from report
                                  start_date=start_date.strftime('%Y-%m-%d %H:%M:%S'),
                                  event_start_date=event_start_date.strftime('%Y-%m-%d %H:%M:%S'),
                                  end_date=end_date.strftime('%Y-%m-%d %H:%M:%S'),
                                  event_end_date=event_end_date.strftime('%Y-%m-%d %H:%M:%S'),
                                  title="AshSec IDR High Level Report")
    _report_write_path = f"{INCOMING_DIR}/{report_file_name}.{report_type}"
    with open(_report_write_path, 'w') as fp:
        fp.write(report_html)
    if not os.path.exists(_report_write_path):
        logging.error(f"failed to write report: {_report_write_path}")
        return False
    logging.info(f"wrote {report_file_name}")

    # generate xlsx document - see also ace_metrics.helpers.dataframes_to_archive_bytes_of_json_files
    filename = f"ACE_IDR_HighLevel_Metrics_{datetime.now().date()}.xlsx"
    with open(f"{INCOMING_DIR}/{filename}.{report_type}", 'wb') as fp:
        fp.write(dataframes_to_xlsx_bytes(tables_for_xlsx))
    if os.path.exists(filename):
        logging.info(f"wrote {filename}")
    ## End High Level Report ##
    ###########################

    # Send configured email notifications
    email_reports_based_on_configuration(config, report_context_map, args.approved_reports)
 
    # save state
    save_report_context_map(report_context_map)

    # delete archived reports
    for report_type in args.approved_reports:
        for report_path in glob.glob(f"{os.path.join(ARCHIVE_DIR)}/*.{report_type}"):
            os.remove(report_path)
            logging.info(f"deleted old report: {report_path}")

    # move new reports to archive
    for report_path in glob.glob(f"{os.path.join(INCOMING_DIR)}/*"):
        archive_path = report_path.replace(INCOMING_DIR_NAME, ARCHIVE_DIR_NAME)
        try:
            os.rename(report_path, archive_path)
        except Exception as e:
            logging.error(f"couldn't archive {report_path}: {e}")
        logging.info(f"archived {report_path} to {archive_path}")

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        logging.critical(f"uncaught exception: {e}")
        write_error_report(f"uncaught exception: {e}")
