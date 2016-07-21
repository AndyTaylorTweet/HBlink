import logging
from logging.config import dictConfig

def config_logging(_logger):
    dictConfig({
        'version': 1,
        'disable_existing_loggers': False,
        'filters': {
        },
        'formatters': {
            'verbose': {
                'format': '%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s'
            },
            'timed': {
                'format': '%(levelname)s %(asctime)s %(message)s'
            },
            'simple': {
                'format': '%(levelname)s %(message)s'
            },
            'syslog': {
                'format': '%(name)s (%(process)d): %(levelname)s %(message)s'
            }
        },
        'handlers': {
            'null': {
                'class': 'logging.NullHandler'
            },
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'simple'
            },
            'console-timed': {
                'class': 'logging.StreamHandler',
                'formatter': 'timed'
            },
            'file': {
                'class': 'logging.FileHandler',
                'formatter': 'simple',
                'filename': _logger['LOG_FILE'],
            },
            'file-timed': {
                'class': 'logging.FileHandler',
                'formatter': 'timed',
                'filename': _logger['LOG_FILE'],
            },
            'syslog': {
                'class': 'logging.handlers.SysLogHandler',
                'formatter': 'syslog',
            }
        },
        'loggers': {
            _logger['LOG_NAME']: {
                'handlers': _logger['LOG_HANDLERS'].split(','),
                'level': _logger['LOG_LEVEL'],
                'propagate': True,
            }
        }
    })

    return logging.getLogger(_logger['LOG_NAME'])