import csv

import gevent
from locust import stats
from locust.env import Environment

from kkugi_load import csv_stats


def _rows(path):
    # Locust는 텍스트 모드 파일에 csv를 써서 Windows에서는 줄 사이에 빈 줄이 생긴다
    with open(path, newline="") as f:
        return [row for row in csv.reader(f) if row]


def _column(rows, name, column, key="Name"):
    """머리글이 있는 rows에서 key 열이 name인 줄들의 column 값."""
    header = rows[0]
    return [r[header.index(column)] for r in rows[1:] if r[header.index(key)] == name]


def _writer(tmp_path):
    env = Environment()
    env.create_local_runner()
    writer = stats.StatsCSVFileWriter(env, stats.PERCENTILES_TO_REPORT, str(tmp_path / "k"), full_history=True)
    return env, writer


def _log_requests(env, count):
    for _ in range(count):
        env.stats.log_request("GET", "/foods", 10, 0)


def test_requests_after_last_interval_are_written_on_close(tmp_path):
    csv_stats.install(60)
    env, writer = _writer(tmp_path)
    greenlet = gevent.spawn(writer.stats_writer)
    gevent.sleep(0.05)  # 첫 기록(요청 0건)까지만 돈다. 다음 기록은 60초 뒤다
    _log_requests(env, 3)
    writer.close_files()
    greenlet.kill()

    requests = _rows(tmp_path / "k_stats.csv")
    assert requests[0][:2] == ["Type", "Name"]
    assert _column(requests, "/foods", "Request Count") == ["3"]
    history = _rows(tmp_path / "k_stats_history.csv")
    assert history[0][0] == "Timestamp"
    # 첫 기록과 종료할 때의 기록이 둘 다 남는다
    assert _column(history, "Aggregated", "Total Request Count") == ["0", "3"]


def test_writer_that_never_ran_still_writes_headers_and_values(tmp_path):
    csv_stats.install(60)
    env, writer = _writer(tmp_path)
    _log_requests(env, 2)
    writer.close_files()

    assert _column(_rows(tmp_path / "k_stats.csv"), "/foods", "Request Count") == ["2"]
    assert _rows(tmp_path / "k_stats_history.csv")[0][0] == "Timestamp"
    assert _rows(tmp_path / "k_failures.csv")[0][0] == "Method"


def test_install_wraps_close_once():
    csv_stats.install(60)
    wrapped = stats.StatsCSVFileWriter.close_files
    csv_stats.install(30)
    assert stats.StatsCSVFileWriter.close_files is wrapped
    assert stats.CSV_STATS_INTERVAL_SEC == 30
