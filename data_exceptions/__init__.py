"""Allows for configurable custom exceptions."""

import re
import importlib
import configparser
import logging
import pandas as pd

_MODULE_PATH_REGEX = re.compile(r'^([^:]+):([^:]+)(?::(.+))?$')
def SPLIT_MODULE_PATH(module_path):
    """Given a MODULE_PATH result, return a tuple of (module, class, instance)."""
    m = _MODULE_PATH_REGEX.match(module_path)
    if m is None:
        return False

    return m.groups()

def apply_alert_data_exceptions(config: configparser.ConfigParser, alerts: pd.DataFrame, exception_list: list) -> pd.DataFrame:
    """Apply configured exceptions to alerts."""

    for exception_key in exception_list:
        exception_function = None
        _module, _func, _instance = SPLIT_MODULE_PATH(config['alert_data_exceptions'].get(exception_key))
        try:
            m = importlib.import_module(_module)
        except Exception as e:
            logging.error(f"unable to import module {_module}: {e}")
            continue
        try:
            exception_function = getattr(m, _func)
        except Exception as e:
            logging.error(f"unable to import function {_func} from module {_module}: {e}")
            continue
        if exception_function is not None:
            alerts = exception_function(alerts)

    return alerts    
