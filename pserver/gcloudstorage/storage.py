# -*- coding: utf-8 -*-
import aiohttp
import asyncio
import base64
import json
import logging
import transaction
import uuid
import multidict

from aiohttp.web import StreamResponse
from datetime import datetime
from datetime import timedelta
from dateutil.tz import tzlocal
from google.cloud import storage
from google.cloud.exceptions import NotFound
from googleapiclient import discovery
from googleapiclient import errors
from googleapiclient import http
from io import BytesIO
from oauth2client.service_account import ServiceAccountCredentials
from persistent import Persistent
from plone.server.browser import Response
from plone.server.events import notify
from plone.server.interfaces import IAbsoluteURL
from plone.server.interfaces import IFileManager
from plone.server.interfaces import IRequest
from plone.server.interfaces import IResource
from plone.server.json.interfaces import IValueToJson
from plone.server.transactions import RequestNotFound
from plone.server.transactions import get_current_request
from plone.server.transactions import tm
from pserver.gcloudstorage.events import FinishGCloudUpload
from pserver.gcloudstorage.events import InitialGCloudUpload
from pserver.gcloudstorage.interfaces import IGCloudBlobStore
from pserver.gcloudstorage.interfaces import IGCloudFile
from pserver.gcloudstorage.interfaces import IGCloudFileField
from zope.component import adapter
from zope.component import getUtility
from zope.interface import implementer
from plone.server import configure
from zope.schema import Object
from zope.schema.fieldproperty import FieldProperty

try:
    from oauth2client import util
except ImportError:
    from oauth2client import _helpers as util

log = logging.getLogger('pserver.storage')

MAX_SIZE = 1073741824

SCOPES = ['https://www.googleapis.com/auth/devstorage.read_write']
UPLOAD_URL = 'https://www.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=resumable'  # noqa
CHUNK_SIZE = 524288
MAX_RETRIES = 5


class GoogleCloudException(Exception):
    pass


@configure.adapter(
    for_=IGCloudFile,
    provides=IValueToJson)
def json_converter(value):
    if value is None:
        return value

    return {
        'filename': value.filename,
        'contenttype': value.contentType,
        'size': value.size,
        'extension': value.extension,
        'md5': value.md5
    }


@configure.adapter(
    for_=(IResource, IRequest, IGCloudFileField),
    provides=IFileManager)
class GCloudFileManager(object):

    def __init__(self, context, request, field):
        self.context = context
        self.request = request
        self.field = field

    async def upload(self):
        """In order to support TUS and IO upload.

        we need to provide an upload that concats the incoming
        """
        file = self.field.get(self.context)
        if file is None:
            file = GCloudFile(contentType=self.request.content_type)
            self.field.set(self.context, file)
            # Its a long transaction, savepoint
            try:
                trns = tm(self.request).get()
            except RequestNotFound:
                trns = transaction.get()
            trns.savepoint()
        if 'X-UPLOAD-MD5HASH' in self.request.headers:
            file._md5hash = self.request.headers['X-UPLOAD-MD5HASH']
        else:
            file._md5hash = None

        if 'X-UPLOAD-EXTENSION' in self.request.headers:
            file._extension = self.request.headers['X-UPLOAD-EXTENSION']
        else:
            file._extension = None

        if 'X-UPLOAD-SIZE' in self.request.headers:
            file._size = int(self.request.headers['X-UPLOAD-SIZE'])
        else:
            raise AttributeError('x-upload-size header needed')

        if 'X-UPLOAD-FILENAME' in self.request.headers:
            file.filename = self.request.headers['X-UPLOAD-FILENAME']
        elif 'X-UPLOAD-FILENAME-B64' in self.request.headers:
            file.filename = base64.b64decode(self.request.headers['X-UPLOAD-FILENAME-B64']).decode("utf-8")
        else:
            file.filename = uuid.uuid4().hex

        await file.initUpload(self.context)
        try:
            data = await self.request.content.readexactly(CHUNK_SIZE)
        except asyncio.IncompleteReadError as e:
            data = e.partial

        count = 0
        while data:
            old_current_upload = file._current_upload
            resp = await file.appendData(data)
            readed_bytes = file._current_upload - old_current_upload

            data = data[readed_bytes:]

            bytes_to_read = readed_bytes

            if resp.status in [200, 201]:
                break
            if resp.status == 308:
                count = 0
                try:
                    data += await self.request.content.readexactly(bytes_to_read)  # noqa
                except asyncio.IncompleteReadError as e:
                    data += e.partial

            else:
                count += 1
                if count > MAX_RETRIES:
                    raise AttributeError('MAX retries error')
        # Test resp and checksum to finish upload
        await file.finishUpload(self.context)

    async def tus_create(self):

        # This only happens in tus-java-client, redirect this POST to a PATCH
        if self.request.headers.get('X-HTTP-Method-Override') == 'PATCH':
            return await self.tus_patch()

        file = self.field.get(self.context)
        if file is None:
            file = GCloudFile(contentType=self.request.content_type)
            self.field.set(self.context, file)
        if 'CONTENT-LENGTH' in self.request.headers:
            file._current_upload = int(self.request.headers['CONTENT-LENGTH'])
        else:
            file._current_upload = 0
        if 'UPLOAD-LENGTH' in self.request.headers:
            file._size = int(self.request.headers['UPLOAD-LENGTH'])
        else:
            raise AttributeError('We need upload-length header')

        if 'UPLOAD-MD5' in self.request.headers:
            file._md5hash = self.request.headers['UPLOAD-MD5']

        if 'UPLOAD-EXTENSION' in self.request.headers:
            file._extension = self.request.headers['UPLOAD-EXTENSION']

        if 'TUS-RESUMABLE' not in self.request.headers:
            raise AttributeError('Its a TUS needs a TUS version')

        if 'UPLOAD-METADATA' not in self.request.headers:
            file.filename = uuid.uuid4().hex
        else:
            filename = self.request.headers['UPLOAD-METADATA']
            file.filename = base64.b64decode(filename.split()[1]).decode('utf-8')

        await file.initUpload(self.context)
        # Location will need to be adapted on aiohttp 1.1.x
        resp = Response(headers=multidict.MultiDict({
            'Location': IAbsoluteURL(self.context, self.request)() + '/@tusupload/' + self.field.__name__,  # noqa
            'Tus-Resumable': '1.0.0',
            'Access-Control-Expose-Headers': 'Location,Tus-Resumable'
        }), status=201)
        return resp

    async def tus_patch(self):
        file = self.field.get(self.context)
        if 'CONTENT-LENGTH' in self.request.headers:
            to_upload = int(self.request.headers['CONTENT-LENGTH'])
        else:
            raise AttributeError('No content-length header')

        if 'UPLOAD-OFFSET' in self.request.headers:
            file._current_upload = int(self.request.headers['UPLOAD-OFFSET'])
        else:
            raise AttributeError('No upload-offset header')
        try:
            data = await self.request.content.readexactly(to_upload)
        except asyncio.IncompleteReadError as e:
            data = e.partial
        count = 0
        while data:
            old_current_upload = file._current_upload
            resp = await file.appendData(data)
            # The amount of bytes that are readed
            if resp.status in [200, 201]:
                # If we finish the current upload is the size of the file
                readed_bytes = file._current_upload - old_current_upload
            else:
                # When it comes from gcloud the current_upload is one number less
                readed_bytes = file._current_upload - old_current_upload + 1

            # Cut the data so there is only the needed data
            data = data[readed_bytes:]

            bytes_to_read = len(data)

            if resp.status in [200, 201]:
                # If we are finished lets close it
                await file.finishUpload(self.context)
                data = None

            if bytes_to_read == 0:
                # We could read all the info
                break

            if bytes_to_read < 262144:
                # There is no enough data to send to gcloud
                break

            if resp.status in [400]:
                # Some error
                break

            if resp.status == 308:
                # We continue resumable
                count = 0
                try:
                    data += await self.request.content.readexactly(bytes_to_read)  # noqa
                except asyncio.IncompleteReadError as e:
                    data += e.partial

            else:
                count += 1
                if count > MAX_RETRIES:
                    raise AttributeError('MAX retries error')
        expiration = file._resumable_uri_date + timedelta(days=7)

        resp = Response(headers=multidict.MultiDict({
            'Upload-Offset': str(file.actualSize()),
            'Tus-Resumable': '1.0.0',
            'Upload-Expires': expiration.isoformat(),
            'Access-Control-Expose-Headers': 'Upload-Offset,Upload-Expires,Tus-Resumable'
        }))
        return resp

    async def tus_head(self):
        file = self.field.get(self.context)
        if file is None:
            raise KeyError('No file on this context')
        head_response = {
            'Upload-Offset': str(file.actualSize()),
            'Tus-Resumable': '1.0.0',
            'Access-Control-Expose-Headers': 'Upload-Offset,Upload-Length,Tus-Resumable'
        }
        if file.size:
            head_response['Upload-Length'] = str(file._size)
        resp = Response(headers=multidict.MultiDict(head_response))
        return resp

    async def tus_options(self):
        resp = Response(headers=multidict.MultiDict({
            'Tus-Resumable': '1.0.0',
            'Tus-Version': '1.0.0',
            'Tus-Max-Size': '1073741824',
            'Tus-Extension': 'creation,expiration'
        }))
        return resp

    async def download(self):
        file = self.field.get(self.context)
        if file is None:
            raise AttributeError('No field value')

        resp = StreamResponse(headers=multidict.MultiDict({
            'CONTENT-DISPOSITION': 'attachment; filename="%s"' % file.filename
        }))
        resp.content_type = file.contentType
        if file.size:
            resp.content_length = file.size
        buf = BytesIO()
        downloader = await file.download(buf)
        await resp.prepare(self.request)
        # response.start(request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            print("Download {}%.".format(int(status.progress() * 100)))
            buf.seek(0)
            data = buf.read()
            resp.write(data)
            await resp.drain()
            buf.seek(0)
            buf.truncate()

        return resp


@implementer(IGCloudFile)
class GCloudFile(Persistent):
    """File stored in a GCloud, with a filename."""

    filename = FieldProperty(IGCloudFile['filename'])

    def __init__(  # noqa
            self,
            contentType='application/octet-stream',
            filename=None):
        self.contentType = contentType
        self._current_upload = 0
        if filename is not None:
            self.filename = filename
            extension_discovery = filename.split('.')
            if len(extension_discovery) > 1:
                self._extension = extension_discovery[-1]
        elif self.filename is not None:
            self.filename = uuid.uuid4().hex

    async def initUpload(self, context):
        """Init an upload.

        self._uload_file_id : temporal url to image beeing uploaded
        self._resumable_uri : uri to resumable upload
        self._uri : finished uploaded image
        """
        util = getUtility(IGCloudBlobStore)
        request = get_current_request()
        if hasattr(self, '_upload_file_id') and self._upload_file_id is not None:  # noqa
            req = util._service.objects().delete(
                bucket=util.bucket, object=self._upload_file_id)
            try:
                req.execute()
            except errors.HttpError:
                pass

        self._upload_file_id = request._site_id + '/' + uuid.uuid4().hex
        init_url = UPLOAD_URL.format(bucket=util.bucket) + '&name=' +\
            self._upload_file_id
        session = aiohttp.ClientSession()

        creator = ','.join([x.principal.id for x
                            in request.security.participations])
        metadata = json.dumps({
            'CREATOR': creator,
            'REQUEST': str(request),
            'NAME': self.filename
        })
        call_size = len(metadata)
        async with session.post(
                init_url,
                headers={
                    'AUTHORIZATION': 'Bearer %s' % util.access_token,
                    'X-Upload-Content-Type': self.contentType,
                    'X-Upload-Content-Length': str(self._size),
                    'Content-Type': 'application/json; charset=UTF-8',
                    'Content-Length': str(call_size)
                },
                data=metadata) as call:
            if call.status != 200:
                text = await call.text()
                raise GoogleCloudException(text)
            self._resumable_uri = call.headers['Location']
        session.close()
        self._current_upload = 0
        self._resumable_uri_date = datetime.now(tz=tzlocal())
        await notify(InitialGCloudUpload(context))

    async def appendData(self, data):
        session = aiohttp.ClientSession()

        content_range = 'bytes {init}-{chunk}/{total}'.format(
            init=self._current_upload,
            chunk=self._current_upload + len(data) - 1,
            total=self._size)
        async with session.put(
                self._resumable_uri,
                headers={
                    'Content-Length': str(len(data)),
                    'Content-Type': self.contentType,
                    'Content-Range': content_range
                },
                data=data) as call:
            text = await call.text()  # noqa
            # assert call.status in [200, 201, 308]
            if call.status == 308:
                self._current_upload = int(call.headers['Range'].split('-')[1])
            if call.status in [200, 201]:
                self._current_upload = self._size
        session.close()
        return call

    def actualSize(self):
        return self._current_upload

    async def finishUpload(self, context):
        util = getUtility(IGCloudBlobStore)
        # It would be great to do on AfterCommit
        # Delete the old file and update the new uri
        if hasattr(self, '_uri') and self._uri is not None:
            req = util._service.objects().delete(
                bucket=util.bucket, object=self._uri)
            try:
                resp = req.execute()  # noqa
            except errors.HttpError:
                pass
        self._uri = self._upload_file_id
        self._upload_file_id = None

        await notify(FinishGCloudUpload(context))

    async def deleteUpload(self):
        if hasattr(self, '_uri') and self._uri is not None:
            req = util._service.objects().delete(
                bucket=util.bucket, object=self._uri)
            resp = req.execute()
            return resp
        else:
            raise AttributeError('No valid uri')

    async def download(self, buf):
        util = getUtility(IGCloudBlobStore)
        if not hasattr(self, '_uri'):
            url = self._upload_file_id
        else:
            url = self._uri
        req = util._service.objects().get_media(
            bucket=util.bucket, object=url)
        downloader = http.MediaIoBaseDownload(buf, req, chunksize=CHUNK_SIZE)
        return downloader

    def _set_data(self, data):
        raise NotImplemented('Only specific upload permitted')

    def _get_data(self):
        raise NotImplemented('Only specific download permitted')

    data = property(_get_data, _set_data)

    @property
    def size(self):
        if hasattr(self, '_size'):
            return self._size
        else:
            return None

    @property
    def md5(self):
        if hasattr(self, '_md5hash'):
            return self._md5hash
        else:
            return None

    @property
    def extension(self):
        if hasattr(self, '_extension'):
            return self._extension
        else:
            return None

    def getSize(self):  # noqa
        return self.size


@implementer(IGCloudFileField)
class GCloudFileField(Object):
    """A NamedBlobFile field."""

    _type = GCloudFile
    schema = IGCloudFile

    def __init__(self, **kw):
        if 'schema' in kw:
            self.schema = kw.pop('schema')
        super(GCloudFileField, self).__init__(schema=self.schema, **kw)


# Configuration Utility

class GCloudBlobStore(object):

    def __init__(self, settings):
        self._json_credentials = settings['json_credentials']
        self._project = settings['project'] if 'project' in settings else None
        self._credentials = ServiceAccountCredentials.from_json_keyfile_name(
            self._json_credentials, SCOPES)
        self._service = discovery.build(
            'storage', 'v1', credentials=self._credentials)
        self._client = storage.Client(
            project=self._project, credentials=self._credentials)
        self._bucket = settings['bucket']
        self._access_token = self._credentials.get_access_token()
        self._creation_access_token = datetime.now()

    @property
    def access_token(self):
        # expires = self._creation_access_token + timedelta(seconds=self._access_token.expires_in)  # noqa
        # expires_margin = datetime.now() - timedelta(seconds=60)

        # if expires_margin < expires:
        #     self._access_token = self._credentials.get_access_token()
        #     self._creation_access_token = datetime.now()
        self._access_token = self._credentials.get_access_token()
        self._creation_access_token = datetime.now()
        return self._access_token.access_token

    @property
    def bucket(self):
        request = get_current_request()
        if '.' in self._bucket:
            char_delimiter = '.'
        else:
            char_delimiter = '_'
        bucket_name = request._site_id.lower() + char_delimiter + self._bucket
        try:
            bucket = self._client.get_bucket(bucket_name)  # noqa
        except NotFound:
            bucket = self._client.create_bucket(bucket_name)  # noqa
            log.warn('We needed to create bucket ' + bucket_name)
        return bucket_name

    async def initialize(self, app=None):
        # No asyncio loop to run
        self.app = app
