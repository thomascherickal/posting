from __future__ import annotations
from pathlib import Path
from string import Template
from typing import Any, Literal, get_args
import httpx
from pydantic import BaseModel, Field, HttpUrl, SecretStr
import rich
import yaml
import os
from posting.tuple_to_multidict import tuples_to_dict
from posting.variables import SubstitutionError

from posting.version import VERSION


HttpRequestMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
VALID_HTTP_METHODS = get_args(HttpRequestMethod)


def str_presenter(dumper, data):
    if data.count("\n") > 0:
        data = "\n".join(
            [line.rstrip() for line in data.splitlines()]
        )  # Remove any trailing spaces, then put it back together again
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(
    str, str_presenter
)  # to use with safe_dump


class Auth(BaseModel):
    type: Literal["basic", "digest"] | None = Field(default=None)
    basic: BasicAuth | None = Field(default=None)
    digest: DigestAuth | None = Field(default=None)


class BasicAuth(BaseModel):
    username: SecretStr = Field(default="")
    password: SecretStr = Field(default="")


class DigestAuth(BaseModel):
    username: SecretStr = Field(default="")
    password: SecretStr = Field(default="")


class Header(BaseModel):
    name: str
    value: str
    enabled: bool = Field(default=True)


class FormItem(BaseModel):
    name: str
    value: str
    enabled: bool = Field(default=True)


class QueryParam(BaseModel):
    name: str
    value: str
    enabled: bool = Field(default=True)


class Cookie(BaseModel):
    name: str
    value: str
    enabled: bool = Field(default=True)

    @classmethod
    def from_httpx(cls, cookies: httpx.Cookies) -> list[Cookie]:
        return [Cookie(name=name, value=value) for name, value in cookies.items()]


class Options(BaseModel):
    follow_redirects: bool = Field(default=True)
    verify_ssl: bool = Field(default=True)
    attach_cookies: bool = Field(default=True)
    proxy_url: str = Field(default="")
    timeout: float = Field(default=5.0)


class RequestBody(BaseModel):
    content: str | None = Field(default=None)
    form_data: list[FormItem] | None = Field(default=None)

    def to_httpx_args(self) -> dict[str, Any]:
        httpx_args: dict[str, Any] = {}
        if self.content:
            httpx_args["content"] = self.content
        if self.form_data:
            # Ensure we don't delete duplicate keys
            httpx_args["data"] = tuples_to_dict(
                [(item.name, item.value) for item in self.form_data]
            )
        return httpx_args


class RequestModel(BaseModel):
    name: str = Field(default="")
    """The name of the request. This is used to identify the request in the UI.
    Before saving a request, the name may be None."""

    description: str = Field(default="")
    """The description of the request."""

    method: HttpRequestMethod = Field(default="GET")
    """The HTTP method of the request."""

    url: str = Field(default="")
    """The URL of the request."""

    path: Path | None = Field(default=None, exclude=True)
    """The path of the request on the file system (i.e. where the yaml is).
    Before saving a request, the path may be None."""

    body: RequestBody | None = Field(default=None)
    """The body of the request."""

    content: str | bytes | None = Field(default=None)
    """The content of the request."""

    auth: Auth | None = Field(default=None)
    """The authentication information for the request."""

    headers: list[Header] = Field(default_factory=list)
    """The headers of the request."""

    params: list[QueryParam] = Field(default_factory=list)
    """The query parameters of the request."""

    cookies: list[Cookie] = Field(default_factory=list, exclude=True)
    """The cookies of the request.
    
    These are excluded because they should not be persisted to the request file."""

    auth: Auth | None = Field(default=None, exclude=True)
    """The auth information for the request."""

    posting_version: str = Field(default=VERSION)
    """The version of Posting."""

    options: Options = Field(default_factory=Options)
    """The options for the request."""

    def apply_template(self, variables: dict[str, Any]) -> None:
        """Apply the template to the request model."""
        try:
            template = Template(self.url)
            self.url = template.substitute(variables)

            template = Template(self.description)
            self.description = template.substitute(variables)
            template = Template(self.options.proxy_url)
            self.options.proxy_url = template.substitute(variables)

            if self.body:
                if self.body.content:
                    template = Template(self.body.content)
                    self.body.content = template.substitute(variables)
                if self.body.form_data:
                    for item in self.body.form_data:
                        template = Template(item.name)
                        item.name = template.substitute(variables)
                        template = Template(item.value)
                        item.value = template.substitute(variables)

            for header in self.headers:
                template = Template(header.name)
                header.name = template.substitute(variables)
                template = Template(header.value)
                header.value = template.substitute(variables)
            for param in self.params:
                template = Template(param.name)
                param.name = template.substitute(variables)
                template = Template(param.value)
                param.value = template.substitute(variables)

            if self.auth:
                if self.auth.basic:
                    template = Template(self.auth.basic.username.get_secret_value())
                    self.auth.basic.username = SecretStr(template.substitute(variables))
                    template = Template(self.auth.basic.password.get_secret_value())
                    self.auth.basic.password = SecretStr(template.substitute(variables))
                if self.auth.digest:
                    template = Template(self.auth.digest.username.get_secret_value())
                    self.auth.digest.username = SecretStr(
                        template.substitute(variables)
                    )
                    template = Template(self.auth.digest.password.get_secret_value())
                    self.auth.digest.password = SecretStr(
                        template.substitute(variables)
                    )
        except (KeyError, ValueError) as e:
            raise SubstitutionError(f"Variable not defined: {e}")

    def to_httpx(self, client: httpx.AsyncClient) -> httpx.Request:
        """Convert the request model to an httpx request."""
        return client.build_request(
            method=self.method,
            url=self.url,
            **(self.body.to_httpx_args() if self.body else {}),
            headers=httpx.Headers(
                [
                    (header.name, header.value)
                    for header in self.headers
                    if header.enabled
                ]
            ),
            params=httpx.QueryParams(
                [(param.name, param.value) for param in self.params if param.enabled]
            ),
            cookies=httpx.Cookies(
                [
                    (cookie.name, cookie.value)
                    for cookie in self.cookies
                    if cookie.enabled
                ]
            ),
        )

    def save_to_disk(self, path: Path) -> None:
        """Save the request model to a YAML file."""
        content = self.model_dump(exclude_defaults=True, exclude_none=True)
        yaml_content = yaml.dump(content, None, sort_keys=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml_content)


class Contact(BaseModel):
    name: str | None = None
    url: HttpUrl | None = None
    email: str | None = None


class License(BaseModel):
    name: str
    url: HttpUrl | None = None


class ExternalDocs(BaseModel):
    description: str | None = None
    url: HttpUrl


class APIInfo(BaseModel):
    title: str
    description: str | None = None
    termsOfService: HttpUrl | None = None
    contact: Contact | None = None
    license: License | None = None
    version: str


class Collection(BaseModel):
    path: Path
    name: str = Field(default="__default__")
    requests: list[RequestModel] = Field(default_factory=list)
    children: list[Collection] = Field(default_factory=list)
    readme: str | None = Field(default=None)

    @classmethod
    def from_openapi_spec(
        cls, path: Path, info: APIInfo, external_docs: ExternalDocs | None = None
    ) -> Collection:
        readme = cls.generate_readme(info, external_docs)
        return cls(path=path, name=info.title, readme=readme)

    @staticmethod
    def generate_readme(
        info: APIInfo, external_docs: ExternalDocs | None = None
    ) -> str:
        readme = f"# {info.title}\n\n"

        if info.description:
            readme += f"{info.description}\n\n"

        readme += f"Version: {info.version}\n\n"

        if info.termsOfService:
            readme += f"Terms of Service: {info.termsOfService}\n\n"

        if info.contact:
            readme += "## Contact Information\n"
            if info.contact.name:
                readme += f"Name: {info.contact.name}\n\n"
            if info.contact.email:
                readme += f"Email: {info.contact.email}\n\n"
            if info.contact.url:
                readme += f"URL: {info.contact.url}\n\n"
            readme += "\n"

        if info.license:
            readme += "## License\n"
            readme += f"Name: {info.license.name}\n\n"
            if info.license.url:
                readme += f"URL: {info.license.url}\n\n"
            readme += "\n"

        if external_docs:
            readme += "## External Documentation\n"
            if external_docs.description:
                readme += f"{external_docs.description}\n\n"
            readme += f"URL: {external_docs.url}\n\n"

        return readme.strip()

    @classmethod
    def from_directory(cls, directory: str) -> Collection:
        """Load all request models into a tree structure from a directory containing .posting.yaml files.

        Args:
            directory_path: The path to the directory containing .posting.yaml files.

        Returns:
            Collection: The root collection containing all loaded requests and subcollections.
        """
        if directory:
            directory_path = Path(directory)
        else:
            directory_path = Path.cwd()
            directory = str(directory_path)

        request_files = directory_path.rglob("*.posting.yaml")
        collection_name = directory_path.name
        root_collection = Collection(name=collection_name, path=directory_path)

        for file_path in request_files:
            try:
                path_string = str(file_path)
                request = load_request_from_yaml(path_string)
                path_parts = (
                    path_string[len(directory) :].strip(os.path.sep).split(os.path.sep)
                )
                current_level = root_collection
                subpath = root_collection.path
                for part in path_parts[:-1]:
                    subpath = subpath / part
                    found = False
                    for child in current_level.children:
                        if child.name == part:
                            current_level = child
                            found = True
                            break

                    if not found:
                        new_collection = Collection(name=part, path=subpath)
                        current_level.children.append(new_collection)
                        current_level = new_collection
                current_level.requests.append(request)
            except Exception as e:
                print(f"Failed to load {file_path}: {e}")

        return root_collection

    def save_to_disk(self, path: Path) -> None:
        """Save the collection to a directory on disk."""
        if self.readme:
            readme_path = path / "README.md"
            readme_path.write_text(self.readme)
            rich.print(f"Saved collection README to {str(readme_path)!r}.")
        for request in self.requests:
            request.save_to_disk(path / f"{request.name}.posting.yaml")
        for child in self.children:
            child.save_to_disk(path / child.name)


def load_request_from_yaml(file_path: str) -> RequestModel:
    """Load a request model from a YAML file.

    Args:
        file_path: The path to the YAML file.

    Returns:
        RequestModel: The request model loaded from the YAML file.
    """
    with open(file_path, "r") as file:
        data = yaml.safe_load(file)
        return RequestModel(**data, path=Path(file_path))
