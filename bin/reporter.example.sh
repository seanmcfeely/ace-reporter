#!/usr/bin/env bash
# just for cron use
# cron example: 0 5 1 * * {HOME_DIR}/bin/reporter.sh high_level 2> {HOME_DIR}/logs/cron.log 2>&1

if [ -z "$1" ]
then
    echo "usage: reporter.sh report_name"
    exit 1
fi

HOME_DIR="/path/to/ace_reporter"
cd $HOME_DIR || { echo "$HOME_DIR does not exist. exiting."; exit 1;}

# proxy?
#bin/proxy_settings.sh

# activate venv
source venv/bin/activate

# execute any reports
python3 reporter.py -r $1
