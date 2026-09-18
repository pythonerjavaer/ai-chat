from backend.tests.test_radar_opportunities import harness  # noqa: F401
from backend import database


def event(h, job, run='done'):
    with database.connect() as connection:
        return h.service.repository.insert_event(
            connection, run_id=run, entity_type='job', entity_id=job['id'],
            external_id=job['external_id'], event_type='NEW', before=None,
            after=job, fields=[], source_id='legacy-search-discovery', now=database.utc_now(),
        )


def test_saved_jobs_are_account_owned_and_ordered(harness):
    h = harness
    a, b = h.insert('one'), h.insert('two')
    base = '/api/future-radar/saved-jobs'
    for job, priority in [(a, 9), (b, 2)]:
        response = h.client.put(base + '/' + job['id'], json={'priority': priority}, headers=h.auth)
        assert response.status_code == 200, response.text
    items = h.client.get(base, headers=h.auth).json()['items']
    assert [item['job']['id'] for item in items] == [b['id'], a['id']]
    other = h.client.post('/api/auth/register', json={
        'username': 'second-user', 'password': 'correct-horse-456', 'privacy_accepted': True,
    }).json()
    auth = {'Authorization': 'Bearer ' + other['access_token']}
    assert h.client.get(base, headers=auth).json()['items'] == []
    assert h.client.patch(base + '/' + a['id'], json={'priority': 1}, headers=auth).status_code == 404
    assert h.client.patch(base + '/' + a['id'], json={'priority': 0}, headers=h.auth).status_code == 422
    assert h.client.patch(base + '/' + a['id'], json={'priority': 1}, headers=h.auth).status_code == 200
    assert h.client.get(base, headers=h.auth).json()['items'][0]['job']['id'] == a['id']
    assert h.client.delete(base + '/' + a['id'], headers=h.auth).status_code == 200
    assert h.client.get('/api/future-radar/opportunities/' + a['id'], headers=h.auth).status_code == 200
    assert h.client.get(base).status_code in (401, 403)


def test_notifications_persist_until_ack_and_do_not_skip_new_arrivals(harness):
    h = harness
    base = '/api/future-radar/notifications'
    old = h.insert('old')
    event(h, old)
    assert h.client.get(base, headers=h.auth).json()['items'] == []
    first = h.insert('first', verification_status='source_screened')
    event(h, first)
    notice = h.client.get(base, headers=h.auth).json()
    assert [i['job']['id'] for i in notice['items']] == [first['id']]
    assert h.client.get(base, headers=h.auth).json() == notice
    second = h.insert('second')
    event(h, second)
    assert h.client.post(base + '/ack', json={'through_event_id': notice['through_event_id']}, headers=h.auth).status_code == 200
    assert [i['job']['id'] for i in h.client.get(base, headers=h.auth).json()['items']] == [second['id']]
    assert h.client.post(base + '/ack', json={'through_event_id': 9999999}, headers=h.auth).status_code == 422


def test_notifications_wait_for_running_scan_even_on_first_visit(harness):
    h = harness
    with database.connect() as connection:
        connection.execute("INSERT INTO radar_runs(id,started_at,created_at,status) VALUES(?,?,?,'running')", ('pending', database.utc_now(), database.utc_now()))
    first = h.insert('still-running')
    event(h, first, run='pending')
    base = '/api/future-radar/notifications'
    assert h.client.get(base, headers=h.auth).json()['items'] == []
    with database.connect() as connection:
        connection.execute("UPDATE radar_runs SET status='success' WHERE id='pending'")
    assert h.client.get(base, headers=h.auth).json()['items'][0]['job']['id'] == first['id']


def test_application_choices_persist_and_are_private_before_grouping_and_pagination(harness):
    from datetime import date, timedelta
    h = harness
    a, b, c = h.insert('apply'), h.insert('skip', closing_date=(date.today() + timedelta(days=2)).isoformat()), h.insert('new-role')
    base = '/api/future-radar/opportunities'
    def set_status(job, status, headers=None):
        return h.client.put(f"{base}/{job['id']}/application", json={'status': status}, headers=headers or h.auth)
    # Populate a prepared cache before changing account data.
    assert h.client.get(base, headers=h.auth).json()['total'] == 3
    assert set_status(a, 'applied').status_code == 200
    assert set_status(b, 'skipped').status_code == 200
    pool = h.client.get(base, headers=h.auth).json()
    assert pool['total'] == 2
    assert pool['stats']['closing_soon'] == 0
    assert {j['id']: j['application_status'] for j in pool['items']} == {a['id']: 'applied', c['id']: 'not_applied'}
    selected = h.client.get(base, params={'application_status': 'applied', 'page_size': 1}, headers=h.auth).json()
    assert selected['total'] == 1 and selected['items'][0]['id'] == a['id']
    groups = h.client.get(base, params={'view': 'companies', 'application_status': 'skipped'}, headers=h.auth).json()
    assert groups['total_companies'] == groups['total_opportunities'] == 1
    assert groups['items'][0]['opportunity_count'] == 1
    assert h.client.get(base, params={'application_status': 'all'}, headers=h.auth).json()['total'] == 3
    # A later role at the same employer is not automatically excluded.
    d = h.insert('future-strategy', title='2027校园招聘战略数据产品岗')
    assert d['id'] in {j['id'] for j in h.client.get(base, headers=h.auth).json()['items']}
    # New repository/caches still read durable personal state.
    from backend.future_radar.repository import RadarRepository
    h.service.repository = RadarRepository(database.connect)
    assert h.client.get(f"{base}/{a['id']}", headers=h.auth).json()['application_status'] == 'applied'
    other = h.client.post('/api/auth/register', json={'username': 'application-other', 'password': 'correct-horse-789', 'privacy_accepted': True}).json()
    auth = {'Authorization': 'Bearer ' + other['access_token']}
    others = h.client.get(base, headers=auth).json()
    assert others['total'] == 4
    assert all(j['application_status'] == 'not_applied' for j in others['items'])
    assert h.client.get(f"{base}/{a['id']}", headers=auth).json()['application_status'] == 'not_applied'
    assert set_status(a, 'planned').status_code == 200
    assert h.client.get(f"{base}/{a['id']}", headers=h.auth).json()['application_status'] == 'planned'
    assert set_status(a, 'not_applied').status_code == 200
    assert h.client.get(base, params={'application_status': 'applied'}, headers=h.auth).json()['total'] == 0
    assert h.client.put(f"{base}/{a['id']}/application", json={'status': 'applied'}).status_code in (401, 403)
    assert set_status(a, 'invented').status_code == 422
    assert set_status({'id': 'missing-job'}, 'applied').status_code == 404
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM radar_jobs').fetchone()[0] == 4
        assert connection.execute('SELECT COUNT(*) FROM radar_events').fetchone()[0] == 0


def test_skipped_job_is_removed_from_saved_jobs_and_notifications(harness):
    h = harness
    base = '/api/future-radar'
    h.client.get(base + '/notifications', headers=h.auth)
    job = h.insert('skip-alert')
    h.client.put(base + '/saved-jobs/' + job['id'], json={'priority': 1}, headers=h.auth)
    h.client.put(base + '/opportunities/' + job['id'] + '/application', json={'status': 'skipped'}, headers=h.auth)
    event(h, job)
    assert h.client.get(base + '/notifications', headers=h.auth).json()['items'] == []
    assert h.client.get(base + '/saved-jobs', headers=h.auth).json()['items'] == []
    # Undoing a skip preserves the saved choice and its ordering.
    h.client.put(base + '/opportunities/' + job['id'] + '/application', json={'status': 'planned'}, headers=h.auth)
    assert h.client.get(base + '/saved-jobs', headers=h.auth).json()['items'][0]['priority'] == 1


def test_application_follows_verified_alias_and_does_not_leak_into_cache(harness):
    h = harness
    original = h.insert('discovery-role', title='2027 校园招聘数据分析岗', official_url='https://careers.example.com/campus/same')
    base = '/api/future-radar/opportunities'
    h.client.put(f"{base}/{original['id']}/application", json={'status': 'applied'}, headers=h.auth)
    verified = h.insert('official-role', source_id='legacy-recruitment-pipeline', verification_status='verified',
                        title=original['title'], official_url=original['official_url'])
    result = h.client.get(base, headers=h.auth).json()
    assert result['total'] == 1
    assert result['items'][0]['id'] == verified['id']
    assert result['items'][0]['application_status'] == 'applied'
    assert h.client.get(f"{base}/{original['id']}", headers=h.auth).json()['application_status'] == 'applied'
    h.client.put(f"{base}/{verified['id']}/application", json={'status': 'not_applied'}, headers=h.auth)
    assert h.client.get(f"{base}/{original['id']}", headers=h.auth).json()['application_status'] == 'not_applied'
    assert h.client.get(base, headers=h.auth).json()['items'][0]['application_status'] == 'not_applied'


def test_applied_history_keeps_expired_closed_and_inactive_source_jobs_without_bookmarks(harness):
    from datetime import date, timedelta
    h = harness
    jobs = [
        h.insert('closed-history', status='closed'),
        h.insert('expired-history', closing_date=(date.today() - timedelta(days=2)).isoformat()),
        h.insert('inactive-history'),
        h.insert('active-history'),
        h.insert('another-active-history'),
    ]
    base = '/api/future-radar/opportunities'
    for job in jobs:
        response = h.client.put(f"{base}/{job['id']}/application", json={'status': 'applied'}, headers=h.auth)
        assert response.status_code == 200, response.text
    with database.connect() as connection:
        connection.execute('UPDATE job_sources SET active=0 WHERE job_id=?', (jobs[2]['id'],))
    query = {'status': 'all', 'application_status': 'applied', 'view': 'jobs',
             'balanced_only': False, 'priority_only': False, 'page_size': 2}
    received = []
    for page in range(1, 4):
        response = h.client.get(base, params={**query, 'page': page}, headers=h.auth)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['total'] == 5
        received.extend(job['id'] for job in payload['items'])
    assert set(received) == {job['id'] for job in jobs}
    assert len(received) == 5
    assert h.client.get('/api/future-radar/saved-jobs', headers=h.auth).json()['items'] == []
    assert h.client.get(base, headers=h.auth).json()['total'] == 2
