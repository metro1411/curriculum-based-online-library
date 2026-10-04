"""Gunicorn settings, read automatically from the working directory.

The first start publishes the bundled prospectus (about 1,500 modules) while
the app loads. Against a remote database that can take longer than
Gunicorn's default 30-second worker timeout, so allow two minutes.
"""

timeout = 120
