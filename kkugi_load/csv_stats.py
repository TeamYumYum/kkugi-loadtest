"""Locust CSV 통계(--csv, --csv-full-history)의 기록 간격을 늘리고, 종료할 때 마지막 값을 남긴다.

Locust 기본 간격은 1초라 며칠씩 돌리면 --csv-full-history 기록이 API마다 하루 86,400줄 쌓인다. 실측은 분 단위로
보면 충분하므로 간격을 늘린다. 그런데 Locust는 종료할 때 CSV를 한 번 더 쓰지 않고 파일만 닫는다. 간격이 1초면
잃는 것이 1초 이하라 드러나지 않지만, 간격을 늘리면 마지막 간격만큼의 통계가 빠진다. 간격보다 짧은 실행은
누적 통계(_stats.csv)가 비어 버린다. 그래서 파일을 닫기 직전에 모든 파일을 한 번 더 쓴다.

Locust 2.47.0(requirements.txt에 고정)의 StatsCSVFileWriter 내부 속성에 기대므로, Locust를 올리면 이 모듈과
tests/test_csv_stats.py부터 확인한다.
"""

from __future__ import annotations

import logging
import time

from locust import stats

log = logging.getLogger("kkugi")


def _write_final(writer: stats.StatsCSVFileWriter) -> None:
    """stats_writer 반복 한 번과 같은 내용을 쓴다. 머리글이 아직 없으면(기록을 한 번도 못 했으면) 함께 쓴다."""
    # 누적 통계는 매번 머리글부터 다시 쓴다
    writer.requests_csv_filehandle.seek(0)
    writer.requests_csv_filehandle.truncate()
    writer.requests_csv_writer.writerow(writer.requests_csv_columns)
    writer._requests_data_rows(writer.requests_csv_writer)

    # 기록은 뒤에 이어 붙인다
    if writer.stats_history_csv_filehandle.tell() == 0:
        writer.stats_history_csv_writer.writerow(writer.stats_history_csv_columns)
    writer._stats_history_data_rows(writer.stats_history_csv_writer, time.time())

    for handle, csv_writer, start, columns, rows in (
        (writer.failures_csv_filehandle, writer.failures_csv_writer, writer.failures_csv_data_start,
         writer.failures_columns, writer._failures_data_rows),
        (writer.exceptions_csv_filehandle, writer.exceptions_csv_writer, writer.exceptions_csv_data_start,
         writer.exceptions_columns, writer._exceptions_data_rows),
    ):
        if start == 0:
            handle.seek(0)
            handle.truncate()
            csv_writer.writerow(columns)
        else:
            handle.seek(start)
            handle.truncate()
        rows(csv_writer)


def install(interval_seconds: float) -> None:
    """기록 간격을 바꾸고, 파일을 닫을 때 마지막 값을 쓰게 한다. 여러 번 불러도 한 번만 감싼다."""
    stats.CSV_STATS_INTERVAL_SEC = interval_seconds
    close = stats.StatsCSVFileWriter.close_files
    if getattr(close, "_kkugi_final_write", False):
        return

    def close_files(self) -> None:
        try:
            _write_final(self)
        except Exception:  # noqa: BLE001 마지막 기록에 실패해도 종료는 막지 않는다
            log.exception("종료할 때 CSV 통계를 쓰지 못했다")
        close(self)

    close_files._kkugi_final_write = True
    stats.StatsCSVFileWriter.close_files = close_files
