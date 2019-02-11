import time
import io
import csv
import json
from typing import List
from .. import core, config, const, exceptions
from ..utils import bulk as bulk_utils
from ..models import bulk as models
from . import base


class Bulk(base.RestService):
    def __init__(self, connection):
        super().__init__(connection, 'jobs/ingest')

    def insert(self, object_name: str, entries: List[dict]) -> 'Job':
        return self._execute_operation('insert', object_name, entries)

    def update(self, object_name: str, entries: List[dict]) -> 'Job':
        return self._execute_operation('update', object_name, entries)

    def upsert(self, object_name: str, entries: List[dict], external_id_field_name: str = 'Id') -> 'Job':
        return self._execute_operation('upsert', object_name, entries, external_id_field_name)

    def select(self, **kwargs):
        raise NotImplementedError

    def delete(self, object_name: str, ids: List[str]) -> 'Job':
        return self._execute_operation('delete', object_name, [{'Id': id} for id in ids])

    def _execute_operation(self, operation: str, object_name: str, entries: List[dict], external_id_field_name: str = None) -> 'Job':
        job_instance = self._create_job(operation, object_name, external_id_field_name)
        job_instance.upload(entries)
        return job_instance.wait()

    def _create_job(self, operation, object_name, external_id_field_name):
        result = self._post(json={
            'columnDelimiter': 'COMMA',
            'contentType': 'CSV',
            'lineEnding': 'LF',
            'object': object_name,
            'operation': operation,
            'externalIdFieldName': external_id_field_name
        })
        return Job(self.connection, result.get('id'))


class BulkObject:
    def __init__(self, object_name, connection):
        self.object_name = object_name
        self.bulk_service = Bulk(connection)

    def insert(self, entries: List[dict]) -> 'Job':
        return self.bulk_service.insert(self.object_name, entries)

    def delete(self, ids: List[str]) -> 'Job':
        return self.bulk_service.delete(self.object_name, ids)

    def update(self, entries: List[dict]) -> 'Job':
        return self.bulk_service.update(self.object_name, entries)

    def upsert(self, entries: List[dict], external_id_field_name='Id') -> 'Job':
        return self.bulk_service.upsert(self.object_name, entries, external_id_field_name)


class Job(base.RestService):
    def __init__(self, connection, job_id):
        super().__init__(connection, 'jobs/ingest/' + job_id)
        self.job_id = job_id

    def _set_state(self, new_state):
        return self._patch(json={'state': new_state})

    def _prepare_data(self, entries):
        return bulk_utils.FilePreparer(entries).get_csv_string()

    def upload(self, entries):
        try:
            self._put('batches', data=self._prepare_data(entries), headers={
                'Content-Type': 'text/csv'
            })
        except json.decoder.JSONDecodeError:
            pass
        self._set_state(const.BULK_STATE_UPLOAD_COMPLETE)
        return True

    def close(self):
        return self._set_state('UploadComplete')

    def abort(self):
        return self._set_state('Aborted')

    def delete(self):
        return self._delete()

    def info(self):
        return self._get()

    def get_state(self):
        return self.info().get('state')

    def is_done(self) -> bool:
        return self.get_state() in const.BULK_STATES_DONE

    def _get_results(self, uri, callback):
        result = self.connection.request('get', url=self._format_url(uri)).text
        reader = csv.reader(io.StringIO(result))
        next(reader, None)
        return [callback(x) for x in reader]

    def get_successful_results(self) -> List[models.ResultRecord]:
        return self._get_results('successfulResults', lambda x: models.SuccessResultRecord(x[0]))

    def get_failed_results(self) -> List[models.ResultRecord]:
        return self._get_results('failedResults', lambda x: models.FailResultRecord(x[0], x[1]))

    def get_unprocessed_records(self) -> List[models.ResultRecord]:
        raise NotImplementedError

    def wait(self):
        while not self.is_done():
            time.sleep(config.POLL_SLEEP_SECONDS)

        if self.get_state() in const.BULK_STATES_FAIL:
            raise exceptions.BulkJobFailedError(self.info().get('errorMessage'))

        return self.get_failed_results() + \
               self.get_successful_results()