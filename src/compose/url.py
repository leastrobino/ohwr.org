# SPDX-FileCopyrightText: 2025 CERN (home.cern)
#
# SPDX-License-Identifier: BSD-3-Clause

"""Represent URLs."""

from functools import partial
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Annotated, Any, Optional
from urllib.parse import quote, urljoin
import warnings

import requests
from pydantic import Field, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


retries = requests.urllib3.util.Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    backoff_factor=0.5,
    retry_after_max=10,
)
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)


@dataclass
class Url:
    """Represent a URL."""

    url: str

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, core_schema_handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """
        Define a custom Pydantic core schema for validation.

        Parameters:
            source_type: Pydantic source type.
            core_schema_handler: Pydantic core schema handler.

        Returns:
            A Pydantic CoreSchema instance.
        """
        return core_schema.no_info_after_validator_function(
            cls._validate,
            core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                cls._serialize,
            ),
        )

    @classmethod
    def _validate(cls, input_value: Any) -> 'Url':
        """
        Validate input value.

        Parameters:
            input_value: Value to validate.

        Returns:
            A Url instance.
        """
        if isinstance(input_value, cls):
            return input_value
        if isinstance(input_value, str):
            try:
                cls._head(input_value)
            except ValueError as head_error:
                warnings.warn(head_error)
            return cls(input_value)
        warnings.warn("Invalid value: '{0}'".format(input_value))

    @classmethod
    def _serialize(cls, url: 'Url') -> str:
        return url.url

    @classmethod
    def _get_auth_headers(cls, url: str) -> dict:
        if url.startswith('https://gitlab.com/'):
            return {'PRIVATE-TOKEN': os.environ.get('GITLAB_TOKEN')}
        if url.startswith('https://gitlab.cern.ch/'):
            return {'PRIVATE-TOKEN': os.environ.get('GITLAB_CERN_TOKEN')}
        return {}

    @classmethod
    def _request(
        cls, method: str, url: str, headers: Optional[dict] = None,
    ) -> requests.Response:
        headers = cls._get_auth_headers(url) | (headers or {})
        error = None
        while True:
            try:
                res = session.request(method, url, headers=headers, timeout=10)
            except requests.exceptions.RequestException as request_error:
                error = request_error
                break
            try:
                res.raise_for_status()
            except requests.exceptions.RequestException as status_error:
                error = status_error
                break
            return res
        raise ValueError("{0} request to '{1}' failed:\n{2}".format(
            method, url, error,
        ))

    @classmethod
    def _head(
        cls, url: str, headers: Optional[dict] = None,
    ) -> requests.Response:
        return cls._request('HEAD', url, headers=headers)

    @classmethod
    def _get(
        cls, url: str, headers: Optional[dict] = None,
    ) -> requests.Response:
        return cls._request('GET', url, headers=headers)


UrlList = Annotated[list[Url], Field(min_length=1)]


@dataclass
class StrictUrl(Url):
    """Represent a reachable URL."""

    @classmethod
    def _validate(cls, input_value: Any) -> 'StrictUrl':
        """
        Validate input value.

        Parameters:
            input_value: Value to validate.

        Returns:
            A StrictUrl instance.

        Raises:
            ValueError: If the provided value is not valid.
        """
        if isinstance(input_value, cls):
            return input_value
        if isinstance(input_value, str):
            cls._head(input_value)
            return cls(input_value)
        raise ValueError("Invalid value: '{0}'".format(input_value))


StrictUrlList = Annotated[list[StrictUrl], Field(min_length=1)]


@dataclass
class UrlContent(StrictUrl, ABC):
    """Abstract class to fetch URL content."""

    text: str

    @classmethod
    @abstractmethod
    def from_url(cls, url: str) -> 'UrlContent':
        """
        Abstract constructor method to fetch content from a URL.

        Parameters:
            url: URL to fetch content from.

        Returns:
            A UrlContent instance.
        """

    @classmethod
    def create(cls, url: str) -> 'UrlContent':
        """
        Return a specific UrlContent instance based on the URL.

        Parameters:
            url: URL to fetch content from.

        Returns:
            A UrlContent instance.
        """
        gitlab = r'^https://(?:gitlab\.com|gitlab\.cern\.ch)/.+?/wikis/.+'
        if re.search(gitlab, url):
            return GitLabWikiPage.from_url(url)
        return GenericUrlContent.from_url(url)

    @classmethod
    def _validate(cls, input_value: Any) -> 'UrlContent':
        """
        Validate input value.

        Parameters:
            input_value: Value to validate.

        Returns:
            A UrlContent instance.

        Raises:
            ValueError: If the provided value is not valid.
        """
        if isinstance(input_value, cls):
            return input_value
        if isinstance(input_value, str):
            return cls.create(input_value)
        raise ValueError("Invalid value: '{0}'".format(input_value))


class GitLabWikiPage(UrlContent):
    """GitLab Wiki page."""

    @classmethod
    def from_url(cls, url: str) -> 'GitLabWikiPage':
        """
        Fetch a GitLab Wiki page from a URL.

        Parameters:
            url: Wiki page URL.

        Returns:
            A GitLabWikiPage instance.

        Raises:
            ValueError: If fetching the wiki page fails.
        """
        exp = (
            r'^https://((?:gitlab\.com|gitlab\.cern\.ch))/' +
            '(.+?)(?:/-)?/wikis/(.+)'
        )
        match = re.search(exp, url)
        api_url = 'https://{0}/api/v4/projects/{1}/wikis/{2}'.format(
            match.group(1),
            quote(match.group(2), safe=''),
            quote(match.group(3), safe=''),
        )
        try:
            text = cls._get(api_url).json()['content']
        except (TypeError, json.JSONDecodeError, KeyError) as json_error:
            raise ValueError('Failed to load JSON:\n{0}'.format(json_error))
        replacer = partial(cls._rel_to_abs, url=url)
        text = re.sub(r'\[(.*?)\]\((.*?)\)', replacer, text)
        return cls(api_url, text)

    @classmethod
    def _rel_to_abs(cls, match, url):
        text = match.group(1)
        link = match.group(2)
        if not re.match(r'^[a-zA-Z]+://', link):
            link = urljoin(url, link)
        return "[{}]({})".format(text, link)


class GenericUrlContent(UrlContent):
    """Generic URL content."""

    @classmethod
    def from_url(cls, url: str) -> 'GenericUrlContent':
        """
        Fetch content from a URL.

        Parameters:
            url: URL to fetch content from.

        Returns:
            A GenericUrlContent instance.
        """
        return cls(url, cls._get(url).text)
