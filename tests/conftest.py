import os
import sys

# Must be set before `app` is imported: the engine is created at import time.
os.environ['CET4_DATABASE_URI'] = 'sqlite://'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app import app, db  # noqa: E402


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app.test_client()
        db.session.remove()
