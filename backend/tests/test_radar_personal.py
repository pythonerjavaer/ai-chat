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
