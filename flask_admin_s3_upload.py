__version__ = '0.1.4'


try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

from io import BytesIO
import os
import os.path as op
import re

import boto3
from io import BytesIO

from werkzeug.datastructures import FileStorage

from wtforms import ValidationError

from flask_admin.form.upload import FileUploadField, ImageUploadInput, \
    thumbgen_filename
from flask_admin._compat import urljoin

from url_for_s3 import url_for_s3

from botocore.exceptions import ClientError


class S3FileUploadField(FileUploadField):
    """
    Inherits from flask-admin FileUploadField, to allow file uploading
    to Amazon S3 (as well as the default local storage).
    """

    # Updated to boto3 which supports IAM credentials in containers.

    def __init__(self, label=None, validators=None, storage_type=None,
                 bucket_name=None, access_key_id=None, token=None,
                 access_key_secret=None, acl='public-read',
                 storage_type_field=None, bucket_name_field=None,
                 static_root_parent=None, region_name=None, **kwargs):
        super(S3FileUploadField, self).__init__(label, validators, **kwargs)

        if storage_type and (storage_type != 's3'):
            raise ValueError(
                'Storage type "%s" is invalid, the only supported storage type'
                ' (apart from default local storage) is s3.' % storage_type
            )

        self.storage_type = storage_type
        self.bucket_name = bucket_name
        self.acl = acl
        self.storage_type_field = storage_type_field
        self.bucket_name_field = bucket_name_field
        self.static_root_parent = static_root_parent

        # Are we in an ECS container? If so, use the IAM role
        if 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI' in os.environ:
            self.access_key_id = None
            self.access_key_secret = None
            self.region_name = None
            self.token = None
        else:
            self.access_key_id = access_key_id
            self.access_key_secret = access_key_secret
            self.region_name = region_name
            self.token = token

    def populate_obj(self, obj, name):
        field = getattr(obj, name, None)
        if field:
            # If field should be deleted, clean it up
            if self._should_delete:
                self._delete_file(field, obj)
                setattr(obj, name, '')

                if self.storage_type_field:
                    setattr(obj, self.storage_type_field, '')
                if self.bucket_name_field:
                    setattr(obj, self.bucket_name_field, '')

                return

        if (self.data and isinstance(self.data, FileStorage)
                and self.data.filename):
            if field:
                self._delete_file(field, obj)

            filename = self.generate_name(obj, self.data)
            temp_file = BytesIO()
            self.data.save(temp_file)
            filename = self._save_file(temp_file, filename)
            # update filename of FileStorage to our validated name
            self.data.filename = filename

            setattr(obj, name, filename)

            if self.storage_type == 's3':
                if self.storage_type_field:
                    setattr(obj, self.storage_type_field, self.storage_type)
                if self.bucket_name_field:
                    setattr(obj, self.bucket_name_field, self.bucket_name)
            else:
                if self.storage_type_field:
                    setattr(obj, self.storage_type_field, '')
                if self.bucket_name_field:
                    setattr(obj, self.bucket_name_field, '')

    def _get_s3_path(self, filename):
        if not self.static_root_parent:
            raise ValueError('S3FileUploadField field requires '
                             'static_root_parent to be set.')

        return re.sub('^/', '', self._get_path(filename).replace(
            self.static_root_parent, ''))

    def _delete_file(self, filename, obj):
        storage_type = getattr(obj, self.storage_type_field, '')
        bucket_name = getattr(obj, self.bucket_name_field, '')

        if not (storage_type and bucket_name):
            return super(S3FileUploadField, self)._delete_file(filename)

        if storage_type != 's3':
            raise ValueError(
                'Storage type "%s" is invalid, the only supported storage type'
                ' (apart from default local storage) is s3.' % storage_type)

        if self.access_key_id is None:
            conn = boto3.client('s3')
        else:
            conn = boto3.client(
                's3',
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.access_key_secret,
                region_name=self.region_name,
                aws_session_token=self.token
            )

        path = self._get_s3_path(filename)

        try:
            conn.delete_objects(
                Bucket=bucket_name,
                Delete={
                    'Objects': [{
                        'Key': path
                    }]
                }
            )
        except boto3.exceptions.S3DeleteError:
            pass

    def _save_file_local(self, temp_file, filename):
        path = self._get_path(filename)
        if not op.exists(op.dirname(path)):
            os.makedirs(os.path.dirname(path), self.permission | 0o111)

        fd = open(path, 'wb')

        # Thanks to:
        # http://stackoverflow.com/a/3253276/2066849
        temp_file.seek(0)
        t = temp_file.read(1048576)
        while t:
            fd.write(t)
            t = temp_file.read(1048576)

        fd.close()

        return filename

    def _save_file(self, temp_file, filename):
        if not (self.storage_type and self.bucket_name):
            return self._save_file_local(temp_file, filename)

        if self.storage_type != 's3':
            raise ValueError(
                'Storage type "%s" is invalid, the only supported storage type'
                ' (apart from default local storage) is s3.'
                % self.storage_type)

        if self.access_key_id is None:
            conn = boto3.client('s3')
        else:
            conn = boto3.client(
                's3',
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.access_key_secret,
                region_name=os.environ.get('AWS_REGION'),
                aws_session_token=self.token
            )

        file = BytesIO()
        file.write(temp_file.getvalue())
        file.seek(0)

        path = self._get_s3_path(filename)

        try:
            conn.put_object(
                Body=file,
                Bucket=self.bucket_name,
                Key=path,
            )
        except boto3.exceptions.S3UploadFailedError:
            pass

        return filename


class S3ImageUploadInput(ImageUploadInput):
    """
    Inherits from flask-admin ImageUploadInput, to render images
    uploaded to Amazon S3 (as well as the default local storage).
    """

    def get_url(self, field):
        if op.isfile(op.join(field.base_path, field.data)):
            return super(S3ImageUploadInput, self).get_url(field)

        if field.thumbnail_size:
            filename = field.thumbnail_fn(field.data)
        else:
            filename = field.data

        if field.url_relative_path:
            filename = urljoin(field.url_relative_path, filename)

        return url_for_s3(field.endpoint, bucket_name=field.bucket_name,
                          filename=filename)


class S3ImageUploadField(S3FileUploadField):
    """
    Revised version of flask-admin ImageUploadField, to allow image
    uploading to Amazon S3 (as well as the default local storage).

    Based loosely on code from:
    http://stackoverflow.com/a/29178240/2066849
    """

    widget = S3ImageUploadInput()

    keep_image_formats = ('PNG',)

    def __init__(self, label=None, validators=None,
                 max_size=None, thumbgen=None, thumbnail_size=None,
                 url_relative_path=None, endpoint='static',
                 **kwargs):
        # Check if PIL is installed
        if Image is None:
            raise ImportError('PIL library was not found')

        self.max_size = max_size
        self.thumbnail_fn = thumbgen or thumbgen_filename
        self.thumbnail_size = thumbnail_size
        self.endpoint = endpoint
        self.image = None
        self.url_relative_path = url_relative_path

        if (not ('allowed_extensions' in kwargs)
                or not kwargs['allowed_extensions']):
            kwargs['allowed_extensions'] = \
                ('gif', 'jpg', 'jpeg', 'png', 'tiff')

        super(S3ImageUploadField, self).__init__(label, validators,
                                               **kwargs)

    def pre_validate(self, form):
        super(S3ImageUploadField, self).pre_validate(form)

        if (self.data and
                isinstance(self.data, FileStorage) and
                self.data.filename):
            try:
                self.image = Image.open(self.data)
            except Exception as e:
                raise ValidationError('Invalid image: %s' % e)

    # Deletion
    def _delete_file(self, filename, obj):
        storage_type = getattr(obj, self.storage_type_field, '')
        bucket_name = getattr(obj, self.bucket_name_field, '')

        super(S3ImageUploadField, self)._delete_file(filename, obj)

        self._delete_thumbnail(filename, storage_type, bucket_name)

    def _delete_thumbnail_local(self, filename):
        path = self._get_path(self.thumbnail_fn(filename))

        if op.exists(path):
            os.remove(path)

    def _delete_thumbnail(self, filename, storage_type, bucket_name):
        if not (storage_type and bucket_name):
            self._delete_thumbnail_local(filename)
            return

        if storage_type != 's3':
            raise ValueError(
                'Storage type "%s" is invalid, the only supported storage type'
                ' (apart from default local storage) is s3.' % storage_type)

        if self.access_key_id is None:
            conn = boto3.client('s3')
        else:
            conn = boto3.client(
                's3',
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.access_key_secret,
                region_name=os.environ.get('AWS_REGION'),
                aws_session_token=self.token
            )

        path = self._get_s3_path(filename)

        try:
            conn.delete_objects(
                Delete={
                    'Objects': [{
                        'Key': path
                    }]
                },
                Bucket=bucket_name
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                pass  # No such object, ignore the error
            else:
                raise  # Rethrow any other exceptions

    # Saving
    def _save_file(self, temp_file, filename):
        if self.storage_type and (self.storage_type != 's3'):
            raise ValueError(
                'Storage type "%s" is invalid, the only supported storage type'
                ' (apart from default local storage) is s3.' % (
                    self.storage_type,))

        # Figure out format
        filename, format = self._get_save_format(filename, self.image)

        if self.image:    # and (self.image.format != format or self.max_size):
            if self.max_size:
                image = self._resize(self.image, self.max_size)
            else:
                image = self.image

            temp_file = BytesIO()
            self._save_image(image, temp_file, format)

        super(S3ImageUploadField, self)._save_file(temp_file, filename)
        temp_file_thumbnail = BytesIO()
        self._save_thumbnail(temp_file_thumbnail, filename, format)

        return filename

    def _save_thumbnail(self, temp_file, filename, format):
        if self.image and self.thumbnail_size:
            self._save_image(self._resize(self.image, self.thumbnail_size),
                             temp_file,
                             format)

            super(S3ImageUploadField, self)._save_file(
                temp_file, self.thumbnail_fn(filename))

    def _resize(self, image, size):
        (width, height, force) = size

        if image.size[0] > width or image.size[1] > height:
            if force:
                return ImageOps.fit(self.image, (width, height),
                                    Image.ANTIALIAS)
            else:
                thumb = self.image.copy()
                thumb.thumbnail((width, height), Image.ANTIALIAS)
                return thumb

        return image

    def _save_image(self, image, temp_file, format='JPEG'):
        if image.mode not in ('RGB', 'RGBA'):
            image = image.convert('RGBA')

        image.save(temp_file, format)

    def _get_save_format(self, filename, image):
        if image.format not in self.keep_image_formats:
            name, ext = op.splitext(filename)
            filename = '%s.jpg' % name
            return filename, 'JPEG'

        return filename, image.format
