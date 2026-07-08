from typing import Any, cast

from flask import request

import services
from controllers.common.agent_app_parameters import get_published_agent_app_feature_dict_and_user_input_form
from controllers.common.errors import (
    FilenameNotExistsError,
    FileTooLargeError,
    NoFileUploadedError,
    TooManyFilesError,
    UnsupportedFileTypeError,
)
from controllers.common.schema import register_schema_models
from controllers.web import web_ns
from controllers.web.error import AgentNotPublishedError, AppUnavailableError
from controllers.web.wraps import WebApiResource
from core.app.app_config.common.parameters_mapping import get_parameters_from_feature_dict
from core.app.apps.agent_app.errors import AgentAppGeneratorError, AgentAppNotPublishedError
from extensions.ext_database import db
from fields.file_fields import FileResponse
from models.model import App, AppMode, EndUser
from services.file_service import FileService

register_schema_models(web_ns, FileResponse)


def _get_published_file_upload_parameters(app_model: App) -> dict[str, Any]:
    """Return the file-upload section exposed by the public app parameters."""
    features_dict: dict[str, Any]
    user_input_form: list[dict[str, Any]]
    if app_model.mode == AppMode.AGENT:
        try:
            features_dict, user_input_form = get_published_agent_app_feature_dict_and_user_input_form(app_model)
        except AgentAppNotPublishedError:
            raise AgentNotPublishedError()
        except AgentAppGeneratorError:
            raise AppUnavailableError()
    elif app_model.mode in {AppMode.ADVANCED_CHAT, AppMode.WORKFLOW}:
        workflow = app_model.workflow
        if workflow is None:
            raise AppUnavailableError()

        features_dict = workflow.features_dict
        user_input_form = workflow.user_input_form(to_old_structure=True)
    else:
        app_model_config = app_model.app_model_config
        if app_model_config is None:
            raise AppUnavailableError()

        features_dict = cast(dict[str, Any], app_model_config.to_dict())
        user_input_form = features_dict.get("user_input_form", [])

    parameters = get_parameters_from_feature_dict(features_dict=features_dict, user_input_form=user_input_form)
    return parameters["file_upload"]


def _is_local_image_upload_allowed(file_upload: dict[str, Any]) -> bool:
    image_config = file_upload.get("image")
    if isinstance(image_config, dict):
        transfer_methods = image_config.get("transfer_methods", [])
        return image_config.get("enabled") is True and "local_file" in transfer_methods

    allowed_file_upload_methods = file_upload.get("allowed_file_upload_methods", [])
    allowed_file_types = file_upload.get("allowed_file_types", [])
    return (
        file_upload.get("enabled") is True
        and "local_file" in allowed_file_upload_methods
        and (not allowed_file_types or "image" in allowed_file_types)
    )


@web_ns.route("/files/upload")
class FileApi(WebApiResource):
    @web_ns.doc("upload_file")
    @web_ns.doc(description="Upload a file for use in web applications")
    @web_ns.doc(
        responses={
            201: "File uploaded successfully",
            400: "Bad request - invalid file or parameters",
            413: "File too large",
            415: "Unsupported file type",
        }
    )
    @web_ns.response(201, "File uploaded successfully", web_ns.models[FileResponse.__name__])
    def post(self, app_model: App, end_user: EndUser):
        """Upload a file for use in web applications.

        Accepts file uploads for use within web applications, enforcing the
        app's published local image-upload policy before validation and storage.

        Args:
            app_model: The associated application model
            end_user: The end user uploading the file

        Form Parameters:
            file: The file to upload (required)
            source: Optional source type (datasets or None)

        Returns:
            dict: File information including ID, URL, and metadata
            int: HTTP status code 201 for success

        Raises:
            NoFileUploadedError: No file provided in request
            TooManyFilesError: Multiple files provided (only one allowed)
            FilenameNotExistsError: File has no filename
            FileTooLargeError: File exceeds size limit
            UnsupportedFileTypeError: File type not supported
        """
        if "file" not in request.files:
            raise NoFileUploadedError()

        if len(request.files) > 1:
            raise TooManyFilesError()

        file = request.files["file"]
        if not file.filename:
            raise FilenameNotExistsError

        if file.mimetype.startswith("image/"):
            file_upload = _get_published_file_upload_parameters(app_model)
            if not _is_local_image_upload_allowed(file_upload):
                raise UnsupportedFileTypeError()

        source = request.form.get("source")
        if source not in ("datasets", None):
            source = None

        try:
            upload_file = FileService(db.engine).upload_file(
                filename=file.filename,
                content=file.stream.read(),
                mimetype=file.mimetype,
                user=end_user,
                source="datasets" if source == "datasets" else None,
            )
        except services.errors.file.FileTooLargeError as file_too_large_error:
            raise FileTooLargeError(file_too_large_error.description)
        except services.errors.file.UnsupportedFileTypeError:
            raise UnsupportedFileTypeError()

        response = FileResponse.model_validate(upload_file, from_attributes=True)
        return response.model_dump(mode="json"), 201
