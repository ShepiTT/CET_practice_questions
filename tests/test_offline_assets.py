"""The packaged UI must load styles and scripts without a network connection."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from app import app, db
from models import Option, Question, QuestionGroup


class ResourceURLs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('href'):
            self.urls.append(attrs['href'])
        if tag in {'script', 'img', 'audio', 'video', 'source', 'iframe', 'embed'}:
            if attrs.get('src'):
                self.urls.append(attrs['src'])
        if tag == 'object' and attrs.get('data'):
            self.urls.append(attrs['data'])


def assert_local(url):
    parts = urlsplit(url)
    assert not parts.scheme and not parts.netloc, f'External UI resource: {url}'


def test_all_page_layouts_load_assets_from_local_server(client):
    source = '2025-06-CET4-1.pdf'
    group = QuestionGroup(source_file=source, group_type='listening',
                          title='Listening: Questions 1 to 1', audio_start=0,
                          transcript='This is an offline practice passage.')
    question = Question(group=group, source_file=source, q_number=1,
                        content='Question 1', correct_answer='A', explanation='First option.')
    question.options = [Option(label=label, text=f'Option {label}') for label in 'ABCD']
    db.session.add(question)
    db.session.commit()

    for route in ('/', f'/practice/{source}', '/special_practice',
                  '/random_practice', '/wrong_questions', '/admin'):
        response = client.get(route)
        assert response.status_code == 200, route
        parser = ResourceURLs()
        parser.feed(response.get_data(as_text=True))
        assert '/static/vendor/bootstrap/bootstrap.min.css' in parser.urls
        assert '/static/vendor/bootstrap/bootstrap.bundle.min.js' in parser.urls
        for url in parser.urls:
            assert_local(url)
            asset = client.get(urljoin(route, url))
            assert asset.status_code == 200, (route, url)
            assert len(asset.data) > 0, (route, url)


def test_local_bootstrap_has_no_external_stylesheet_dependencies(client):
    css_url = '/static/vendor/bootstrap/bootstrap.min.css'
    response = client.get(css_url)
    assert response.status_code == 200
    assert response.mimetype == 'text/css'
    css = response.get_data(as_text=True)
    assert 'Bootstrap  v5.3.0' in css
    # Bootstrap's icons use inline data URLs; any file-based CSS dependency must
    # also be served by the local app, rather than silently reaching a CDN.
    dependencies = re.findall(r'url\(\s*[\'"]?([^\s\'"\)]+)', css, flags=re.I)
    dependencies += re.findall(r'@import\s+[\'"]([^\'"]+)', css, flags=re.I)
    for dependency in dependencies:
        if dependency.startswith(('data:', '#')):
            continue
        assert_local(dependency)
        assert client.get(urljoin(css_url, dependency)).status_code == 200

    bundle = client.get('/static/vendor/bootstrap/bootstrap.bundle.min.js')
    assert bundle.status_code == 200
    assert bundle.mimetype in {'text/javascript', 'application/javascript'}
    assert 'Bootstrap v5.3.0' in bundle.get_data(as_text=True)
    license_text = (Path(app.static_folder) / 'vendor/bootstrap/LICENSE').read_text()
    assert 'The MIT License (MIT)' in license_text
    assert 'The Bootstrap Authors' in license_text
