import os
import sys

import httpx
from sys import stderr
from multiprocessing import cpu_count
from math import ceil
import base64

import asyncio
from aiomultiprocess import Pool
from typing import List, Optional
from pathlib import Path
from pydantic import BaseModel
import aiofiles
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from ultra.sfjwt import CredentialModel, load_credentials
from ultra.file_operations import combine_files

from tempfile import gettempdir
import shutil


class Organization(BaseModel):
    org_id: str
    client_id: str
    base_url: str


class Batch(BaseModel):
    base_path: str
    batch_number: int
    job_id: str
    batch_start: Optional[str] = None
    batch_size: int
    api_version: str
    object: str
    file_name: Optional[str]
    download_path: str = "./data"
    status: str = "NEW"
    message: Optional[str] = None
    downloaded_file_path: Optional[str]
    attempt_count: int = 0


class CompletedJob(BaseModel):
    id: str
    batches: List[Batch]


def get_query_job(
    job_id: str,
    version: str,
    client: httpx.Client = None,
    credentials: CredentialModel = None,
):
    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
        )
    query_path = f"services/data/v{version}/jobs/query/{job_id}"
    data = client.get(
        f"{query_path}",
    )
    if data.status_code != 200:
        print(data.content.decode(), file=stderr)
    return data.json()


def create_query_job(
    query: str,
    version: str,
    operation: str = "query",
    client: httpx.Client = None,
    credentials: CredentialModel = None,
):
    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                credentials.client_timeout, connect=credentials.client_connect_timeout
            ),
        )

    query_path = f"services/data/v{version}/jobs/query"

    body = {"operation": operation, "query": query}

    data = client.post(
        f"{query_path}",
        json=body,
    )
    if data.status_code != 200 and data.status_code != 201:
        print(data.content.decode(), file=stderr)
    return data.json()


def create_batch_locators(
        job_id: str,
        max_records: int,
        locator: Optional[str],
        version: str,
        client: httpx.Client = None,
        credentials: CredentialModel = None,
    ):
    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
        )
    query_path = f"/services/data/v{version}/jobs/query/{job_id}/results"

    if locator is not None:
        data = client.head(
            f"{query_path}",
            params={
                "maxRecords": max_records,
                "locator": locator,
            }
        )
    else:
        data = client.head(
            f"{query_path}",
            params={
                "maxRecords": max_records,

            }
        )
    if data.status_code != 200 and data.status_code != 201:
        print("Content",data.content, file=stderr)
        print('Locator:',locator)
        print('Max Records:',max_records)
        raise Exception(f"{data.status_code}:Something went wrong to get batch locators")

    locator = data.headers['sforce-locator']
    return locator


def get_query_data(
    job_id: str,
    locator: str,
    max_records: int,
    version: str,
    client: httpx.Client = None,
    credentials: CredentialModel = None,
):
    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
        )
    query_path = f"/services/data/v{version}/jobs/query/{job_id}/results"

    data = client.get(
        f"{query_path}",
        params={
            "maxRecords": max_records,
            "locator": locator,
        },
    )
    if data.status_code != 200 and data.status_code != 201:
        print(data.content.decode(), file=stderr)

    return data.content.decode()


async def a_get_query_data(
    batch: Batch,
    async_client: httpx.AsyncClient = None,
    credentials: CredentialModel = None,
    max_attempts: int = int(os.getenv("SFDC_MAX_DOWNLOAD_ATTEMPTS", 20)),
):
    if credentials is None:
        credentials = load_credentials()
    if async_client is None:
        async_client = httpx.AsyncClient(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                credentials.download_timeout, connect=credentials.client_connect_timeout
            ),
        )

    query_path = (
        f"/services/data/v{batch.api_version}/jobs/query/{batch.job_id}/results"
    )
    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(httpx.ReadTimeout),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=4, max=60),
        ):
            params = {
                         "maxRecords": batch.batch_size,
                         "locator": batch.batch_start,
                     }
            if batch.batch_start is None:
                params.pop("locator")
                # del params["locator"]
            with attempt:
                batch.attempt_count = attempt.retry_state.attempt_number
                data = await async_client.get(
                    f"{query_path}",
                    params=params
                )
    except RetryError as e:
        batch.status = "FAILED"
        batch.message = f"Error occurred while downloading job data after : {str(e)}"
        return batch
    finally:
        await async_client.aclose()
    if data.status_code != 200 and data.status_code != 201:
        # print(data.content.decode(), file=stderr)
        batch.status = "FAILED"
        batch.message = (
            f"Error occurred while downloading job data: {data.content.decode()}"
        )
        return batch

    data_directory = Path(batch.download_path)
    data_directory.mkdir(exist_ok=True)
    file_path = Path(data_directory, f"{batch.job_id}_{batch.batch_number}.csv")

    async with aiofiles.open(file_path, mode="w") as file_out:
        await file_out.write(data.content.decode())

    batch.status = "COMPLETE"
    batch.message = f"{file_path} download complete"
    batch.downloaded_file_path = file_path
    batch.file_name = f"{batch.job_id}_{batch.batch_number}.csv"

    return batch


async def pull_batches(lots: List[Batch]) -> List[Batch]:
    batches: List[Batch] = []

    async with Pool() as pool:
        async for result in pool.map(a_get_query_data, lots):
            batches.append(result)
    return batches


def download_query_data(
    job_id: str,
    version: str = "53.0",
    download_path: str = "./data",
    batch_size: int = 10000,
    dry_run: bool = False,
):
    job_data = get_query_job(job_id=job_id, version=version)
    record_count = job_data.get("numberRecordsProcessed")
    credentials = load_credentials()
    if record_count == 0:
        print("Record Count is 0, No results to process", file=stderr)
        exit()

    if batch_size is None or batch_size == 0:
        batch_size = ceil(job_data.get("numberRecordsProcessed") / cpu_count())


    # locator = create_batch_locators(job_id=job_id, max_records=batch_size, locator='', version=version)
    batch_number = 1
    lots = [Batch(
            batch_number=batch_number,
            batch_size=batch_size,
            job_id=job_id,
            api_version=version,
            base_path=credentials.instance_url,
            object=job_data.get("object"),
            download_path=download_path
    )]
    locator = create_batch_locators(job_id=job_id, max_records=batch_size, locator=None, version=version)
    # lots = []
    for i in range(batch_size, record_count, batch_size):
        batch_number = batch_number + 1
        lots.append(
                    Batch(
                    batch_number=batch_number,
                    batch_start=locator,
                    batch_size=batch_size,
                    job_id=job_id,
                    api_version=version,
                    base_path=credentials.instance_url,
                    object=job_data.get("object"),
                    download_path=download_path
                    )
                )
        locator = create_batch_locators(job_id=job_id, max_records=batch_size, locator=locator, version=version)
    # lots = [
    #     Batch(
    #         batch_start= locator if locator else '',
    #         batch_size=batch_size,
    #         job_id=job_id,
    #         api_version=version,
    #         base_path=credentials.instance_url,
    #         object=job_data.get("object"),
    #         download_path=download_path,
    #     )
    #     #TODO
    #     # Head request: grab first chunk->
    #     for i in range(0, job_data.get("numberRecordsProcessed"), batch_size)
    # ]

    if dry_run:
        return CompletedJob(id=job_id, batches=lots).json(indent=2)

    return CompletedJob(id=job_id, batches=asyncio.run(pull_batches(lots=lots))).json(
        indent=2
    )


def get_job(
    job_id: str,
    version: str,
    client: httpx.Client = None,
    credentials: CredentialModel = None,
):
    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                credentials.client_timeout, connect=credentials.client_connect_timeout
            ),
        )
    data = client.get(
        f"services/data/v{version}/jobs/query/{job_id}",
    )

    if data.status_code == 404:
        data = client.get(f"services/data/v{version}/jobs/ingest/{job_id}")
    if data.status_code != 200:
        print(data.content.decode(), file=stderr)
    return data.json()


def create_ingest_job(
    object_name: str,
    operation: str,
    external_id_field_name: str,
    version: str,
    client: httpx.Client = None,
    credentials: CredentialModel = None,
):
    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Accept": "application/json",
            },
        )

    query_path = f"services/data/v{version}/jobs/ingest"

    body = {"operation": operation, "object": object_name}
    if operation == "upsert":
        if external_id_field_name is None or external_id_field_name == "":
            raise RuntimeError(
                "external Id field name must be provided when performing upsert."
            )
        body["externalIdFieldName"] = external_id_field_name

    data = client.post(
        f"{query_path}",
        json=body,
    )
    if data.status_code != 200 and data.status_code != 201:
        print(data.content.decode(), file=stderr)
    return data.json()


def load_ingest_job_data(
    job_id: str,
    file_path: str,
    version: str,
    client: httpx.Client = None,
    credentials: CredentialModel = None,
) -> Batch:

    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
            },
        )
    client.headers.update({"Content-Type": "text/csv"})
    query_path = f"services/data/v{version}/jobs/ingest/{job_id}"

    with open(file_path, "r") as file_in:
        data = client.put(f"{query_path}/batches", content=file_in.read(), timeout=None)
        if data.status_code != 200 and data.status_code != 201:
            message = data.content.decode()

        else:
            message = f"Batch: {file_path} loaded."

    client.headers.update({"Content-Type": "application/json"})
    payload = {
        "state": "UploadComplete" if data.status_code in (200, 201) else "Aborted"
    }

    result = client.patch(
        f"{query_path}",
        json=payload,
        timeout=None,
    )
    payload["id"] = job_id
    payload["url"] = f"{query_path}"
    payload["file_path"] = file_path
    payload["message"] = message
    payload["status_code"] = result.status_code
    return payload


def ingest_job_data_batches(
    object_name: str,
    operation: str,
    path_or_file: str,
    pattern: str,
    batch_size: int,
    version: str,
    external_id_field_name: str = None,
    working_directory: str = None,
    client: httpx.Client = None,
    credentials: CredentialModel = None,
):

    if working_directory is None:
        working_directory = Path(gettempdir(), object_name)
    if working_directory.exists() and working_directory.is_dir():
        # working_directory.rmdir()
        shutil.rmtree(working_directory)
    working_directory.mkdir(parents=True)

    if credentials is None:
        credentials = load_credentials()
    if client is None:
        client = httpx.Client(
            base_url=credentials.instance_url,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
        )

    file_task = combine_files(
        path_or_file=path_or_file,
        pattern=pattern,
        output_directory=working_directory,
        file_size_limit=batch_size,
    )

    if file_task.status != "success":
        raise RuntimeError(f"Combining files failed: {file_task.message}")

    ingest_job_results = []

    for file_path in file_task.payload:

        bulk_job = create_ingest_job(
            object_name=object_name,
            operation=operation.lower(),
            external_id_field_name=external_id_field_name,
            version=version,
            client=client,
            credentials=credentials,
        )

        ingest_job_results.append(
            load_ingest_job_data(
                job_id=bulk_job.get("id"),
                file_path=file_path,
                version=version,
                client=client,
                credentials=credentials,
            )
        )
    return ingest_job_results

if __name__ == "__main__":
    pass
