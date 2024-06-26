import os
from collections import defaultdict

import google.auth
from google.cloud import trace_v2

from opencensus.common.monitored_resource import (
    aws_identity_doc_utils,
    gcp_metadata_config,
    k8s_utils,
    monitored_resource,
)
from opencensus.common.transports.async_ import AsyncTransport
from opencensus.common.version import __version__
from opencensus.trace import attributes_helper, base_exporter, span_data
from opencensus.trace.attributes import Attributes

# Agent
AGENT = "opencensus-python [{}]".format(__version__)

# Environment variable set in App Engine when vm:true is set.
_APPENGINE_FLEXIBLE_ENV_VM = "GAE_APPENGINE_HOSTNAME"

# Environment variable set in App Engine when env:flex is set.
_APPENGINE_FLEXIBLE_ENV_FLEX = "GAE_INSTANCE"

# GAE common attributes
# See: https://cloud.google.com/appengine/docs/flexible/python/runtime#
#      environment_variables
GAE_ATTRIBUTES = {
    "GAE_VERSION": "g.co/gae/app/version",
    # Note that as of June 2018, the GAE_SERVICE variable needs to map
    # to the g.co/gae/app/module attribute in order for the stackdriver
    # UI to properly filter by 'service' - kinda inconsistent...
    "GAE_SERVICE": "g.co/gae/app/module",
    "GOOGLE_CLOUD_PROJECT": "g.co/gae/app/project",
    "GAE_INSTANCE": "g.co/gae/app/instance",
    "GAE_MEMORY_MB": "g.co/gae/app/memory_mb",
    "PORT": "g.co/gae/app/port",
}

# resource label structure
RESOURCE_LABEL = "g.co/r/%s/%s"

def fix_attribute_map(attributes):
    if "attributeMap" in attributes:
        attributes["attribute_map"] = attributes["attributeMap"]
        del attributes["attributeMap"]
    return attributes

def _update_attr_map(span, attrs):
    attr_map = span.get("attributes", {}).get("attributeMap", {})
    attr_map.update(attrs)
    span["attributes"]["attributeMap"] = attr_map


def set_attributes(trace):
    """Automatically set attributes for Google Cloud environment."""
    spans = trace.get("spans")
    for span in spans:
        if span.get("attributes") is None:
            span["attributes"] = {}

        if is_gae_environment():
            set_gae_attributes(span)

        set_common_attributes(span)

        set_monitored_resource_attributes(span)


def set_monitored_resource_attributes(span):
    """Set labels to span that can be used for tracing.
    :param span: Span object
    """
    resource = monitored_resource.get_instance()
    if resource is None:
        return

    resource_type = resource.get_type()
    resource_labels = resource.get_labels()

    def set_attribute_label(attribute_key, label_key, label_value_prefix=""):
        """Add the attribute to the span attribute map.

        Update the span attribute map (`span['attributes']['attributeMap']`) to
        include a given resource label.
        """
        if attribute_key not in resource_labels:
            return

        pair = {
            RESOURCE_LABEL % (resource_type, label_key): label_value_prefix
            + resource_labels[attribute_key]
        }
        pair_attrs = Attributes(pair).format_attributes_json().get("attributeMap")

        _update_attr_map(span, pair_attrs)

    if resource_type == "k8s_container":
        set_attribute_label(gcp_metadata_config.PROJECT_ID_KEY, "project_id")
        set_attribute_label(k8s_utils.CLUSTER_NAME_KEY, "cluster_name")
        set_attribute_label(k8s_utils.CONTAINER_NAME_KEY, "container_name")
        set_attribute_label(k8s_utils.NAMESPACE_NAME_KEY, "namespace_name")
        set_attribute_label(k8s_utils.POD_NAME_KEY, "pod_name")
        set_attribute_label(gcp_metadata_config.ZONE_KEY, "location")

    elif resource_type == "gce_instance":
        set_attribute_label(gcp_metadata_config.PROJECT_ID_KEY, "project_id")
        set_attribute_label(gcp_metadata_config.INSTANCE_ID_KEY, "instance_id")
        set_attribute_label(gcp_metadata_config.ZONE_KEY, "zone")

    elif resource_type == "aws_ec2_instance":
        set_attribute_label(aws_identity_doc_utils.ACCOUNT_ID_KEY, "aws_account")
        set_attribute_label(aws_identity_doc_utils.INSTANCE_ID_KEY, "instance_id")
        set_attribute_label(
            aws_identity_doc_utils.REGION_KEY, "region", label_value_prefix="aws:"
        )


def set_common_attributes(span):
    """Set the common attributes."""
    common = {
        attributes_helper.COMMON_ATTRIBUTES.get("AGENT"): AGENT,
    }
    common_attrs = Attributes(common).format_attributes_json().get("attributeMap")

    _update_attr_map(span, common_attrs)


def set_gae_attributes(span):
    """Set the GAE environment common attributes."""
    for env_var, attribute_key in GAE_ATTRIBUTES.items():
        attribute_value = os.environ.get(env_var)

        if attribute_value is not None:
            pair = {attribute_key: attribute_value}
            pair_attrs = Attributes(pair).format_attributes_json().get("attributeMap")

            _update_attr_map(span, pair_attrs)


def is_gae_environment():
    """Return True if the GAE related env vars is detected."""
    if (
        _APPENGINE_FLEXIBLE_ENV_VM in os.environ
        or _APPENGINE_FLEXIBLE_ENV_FLEX in os.environ
    ):
        return True


class StackdriverExporter(base_exporter.Exporter):
    """A exporter that send traces and trace spans to Google Cloud Stackdriver
    Trace.

    :type client: :class: `~google.cloud.trace.client.Client`
    :param client: Stackdriver Trace client.

    :type project_id: str
    :param project_id: project_id to create the Trace client.

    :type transport: :class:`type`
    :param transport: Class for creating new transport objects. It should
                      extend from the base_exporter :class:`.Transport` type
                      and implement :meth:`.Transport.export`. Defaults to
                      :class:`.AsyncTransport`. The other option is
                      :class:`.SyncTransport`.
    """

    def __init__(self, client=None, project_id=None, transport=AsyncTransport):
        # The client will handle the case when project_id is None
        if client is None:
            credentials, _ = google.auth.default()
            client_options = {"api_endpoint": "cloudtrace.googleapis.com"}
            client = trace_v2.TraceServiceClient(
                credentials=credentials,
                client_options=client_options,
            )

        self.client = client
        self.project_id = project_id
        self.transport = transport(self)

    def emit(self, span_datas):
        """
        :type span_datas: list of :class:
            `~opencensus.trace.span_data.SpanData`
        :param list of opencensus.trace.span_data.SpanData span_datas:
            SpanData tuples to emit
        """
        project = "projects/{}".format(self.project_id)

        # Map each span data to it's corresponding trace id
        trace_span_map = defaultdict(list)
        for sd in span_datas:
            trace_span_map[sd.context.trace_id] += [sd]

        stackdriver_spans = []
        # Write spans to Stackdriver
        for _, sds in trace_span_map.items():
            # convert to the legacy trace json for easier refactoring
            # TODO: refactor this to use the span data directly
            trace = span_data.format_legacy_trace_json(sds)
            stackdriver_spans.extend(self.translate_to_stackdriver(trace))

        self.client.batch_write_spans(name=project, spans=stackdriver_spans)

    def export(self, span_datas):
        """
        :type span_datas: list of :class:
            `~opencensus.trace.span_data.SpanData`
        :param list of opencensus.trace.span_data.SpanData span_datas:
            SpanData tuples to export
        """
        self.transport.export(span_datas)

    def translate_to_stackdriver(self, trace):
        """Translate the spans json to Stackdriver format.

        See: https://cloud.google.com/trace/docs/reference/v2/rest/v2/
             projects.traces/batchWrite

        :type trace: dict
        :param trace: Trace dictionary

        :rtype: dict
        :returns: Spans in Google Cloud StackDriver Trace format.
        """
        set_attributes(trace)
        spans_json = trace.get("spans")
        trace_id = trace.get("traceId")

        for span in spans_json:
            span_name = "projects/{}/traces/{}/spans/{}".format(
                self.project_id, trace_id, span.get("spanId")
            )

            span_json = {
                "name": span_name,
                "display_name": span.get("displayName"),
                "start_time": span.get("startTime"),
                "end_time": span.get("endTime"),
                "span_id": str(span.get("spanId")),
                "attributes": self.map_attributes(fix_attribute_map(span.get("attributes"))),
                "links": span.get("links"),
                "status": span.get("status"),
                "stack_trace": span.get("stackTrace"),
                "time_events": span.get("timeEvents"),
                "same_process_as_parent_span": span.get("sameProcessAsParentSpan"),
                "child_span_count": span.get("childSpanCount"),
            }

            if span.get("parentSpanId") is not None:
                parent_span_id = str(span.get("parentSpanId"))
                span_json["parent_span_id"] = parent_span_id

            yield span_json

    def map_attributes(self, attribute_map):
        if attribute_map is None:
            return attribute_map

        for key, value in attribute_map.items():
            if key != "attributeMap":
                continue
            for attribute_key in list(value.keys()):
                if attribute_key in ATTRIBUTE_MAPPING:
                    new_key = ATTRIBUTE_MAPPING.get(attribute_key)
                    value[new_key] = value.pop(attribute_key)
                    if new_key == "/http/status_code":
                        # workaround: Stackdriver expects status to be str
                        hack = value[new_key]
                        hack = hack["int_value"]
                        if not isinstance(hack, int):
                            hack = hack["value"]
                        value[new_key] = {
                            "string_value": {
                                "truncated_byte_count": 0,
                                "value": str(hack),
                            }
                        }

        return attribute_map


ATTRIBUTE_MAPPING = {
    "component": "/component",
    "error.message": "/error/message",
    "error.name": "/error/name",
    "http.client_city": "/http/client_city",
    "http.client_country": "/http/client_country",
    "http.client_protocol": "/http/client_protocol",
    "http.client_region": "/http/client_region",
    "http.host": "/http/host",
    "http.method": "/http/method",
    "http.redirected_url": "/http/redirected_url",
    "http.request_size": "/http/request/size",
    "http.response_size": "/http/response/size",
    "http.status_code": "/http/status_code",
    "http.url": "/http/url",
    "http.user_agent": "/http/user_agent",
    "pid": "/pid",
    "stacktrace": "/stacktrace",
    "tid": "/tid",
    "grpc.host_port": "/grpc/host_port",
    "grpc.method": "/grpc/method",
}
