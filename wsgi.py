# This file contains the WSGI configuration required to serve up your
# web application at http://<your-username>.pythonanywhere.com/
# It works by setting the variable 'application' to a WSGI app object.

import sys

# Add your project's directory to the Python path.
# Replace '<your-username>' and '<your-repo-name>' with your actual details.
project_home = '/home/jjjjzzzz/stuffs_seq_flask' 
if project_home not in sys.path:
    sys.path = [project_home] + sys.path

# Import the 'app' object from your flask_app.py file
from flask_app import app as application
