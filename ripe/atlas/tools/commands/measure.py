from __future__ import print_function, absolute_import

import re
import sys

from collections import OrderedDict

from ripe.atlas.cousteau import (
    Ping, Traceroute, Dns, Sslcert, Http, Ntp, AtlasSource, AtlasCreateRequest)
from ripe.atlas.sagan.dns import Message

from ..exceptions import RipeAtlasToolsException
from ..helpers.colours import colourise
from ..helpers.validators import ArgumentType
from ..renderers import Renderer
from ..settings import conf
from ..streaming import Stream, CaptureLimitExceeded
from .base import Command as BaseCommand
from .base import Factory as BaseFactory


class Command(BaseCommand):

    NAME = "measure"

    DESCRIPTION = "Create a measurement and optionally wait for the results"

    CREATION_CLASSES = OrderedDict((
        ("ping", Ping),
        ("traceroute", Traceroute),
        ("dns", Dns),
        ("ssl", Sslcert),
        ("http", Http),
        ("ntp", Ntp)
    ))

    def __init__(self, *args, **kwargs):

        self._type = None
        self._is_oneoff = True

        BaseCommand.__init__(self, *args, **kwargs)

    def _modify_parser_args(self, args):

        kinds = self.CREATION_CLASSES.keys()
        error = (
            "Usage: ripe-atlas measure <{}> [options]\n"
            "\n"
            "  Example: ripe-atlas measure ping --target example.com"
            "".format("|".join(kinds))
        )

        if not args:
            raise RipeAtlasToolsException(error)

        if args[0] not in self.CREATION_CLASSES.keys():
            raise RipeAtlasToolsException(error)
        self._type = args.pop(0)

        if not args:
            args.append("--help")

        return BaseCommand._modify_parser_args(self, args)

    def add_arguments(self):

        self.parser.add_argument(
            "--renderer",
            choices=Renderer.get_available(),
            help="The renderer you want to use. If this isn't defined, an "
                 "appropriate renderer will be selected."
        )
        self.parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not create the measurement, only show its definition."
        )

        # Standard for all types

        self.parser.add_argument(
            "--auth",
            type=str,
            default=conf["authorisation"]["create"],
            help="The API key you want to use to create the measurement"
        )
        self.parser.add_argument(
            "--af",
            type=int,
            choices=(4, 6),
            help="The address family, either 4 or 6"
        )
        self.parser.add_argument(
            "--description",
            type=str,
            default=conf["specification"]["description"],
            help="A free-form description"
        )
        self.parser.add_argument(  # Most types
            "--target",
            type=ArgumentType.ip_or_domain,
            help="The target, either a domain name or IP address.  If creating "
                 "a DNS measurement, the absence of this option will imply "
                 "that you wish to use the probe's resolver."
        )
        self.parser.add_argument(
            "--no-report",
            action="store_true",
            help="Don't wait for a response from the measurement, just return "
                 "the URL at which you can later get information about the "
                 "measurement."
        )

        self.parser.add_argument(
            "--interval",
            type=int,
            help="Rather than run this measurement as a one-off (the default), "
                 "create this measurement as a recurring one, with an interval "
                 "of n seconds between attempted measurements. This option "
                 "implies --no-report."
        )

        origins = self.parser.add_mutually_exclusive_group()
        origins.add_argument(
            "--from-area",
            type=str,
            choices=("WW", "West", "North-Central", "South-Central",
                     "North-East", "South-East"),
            help="The area from which you'd like to select your probes."
        )
        origins.add_argument(
            "--from-country",
            type=ArgumentType.country_code,
            metavar="COUNTRY",
            help="The two-letter ISO code for the country from which you'd "
                 "like to select your probes. Example: --from-country=GR"
        )
        origins.add_argument(
            "--from-prefix",
            type=str,
            metavar="PREFIX",
            help="The prefix from which you'd like to select your probes. "
                 "Example: --from-prefix=82.92.0.0/14"
        )
        origins.add_argument(
            "--from-asn",
            type=ArgumentType.integer_range(1, 2**32),
            metavar="ASN",
            help="The ASN from which you'd like to select your probes. "
                 "Example: --from-asn=3333"
        )
        origins.add_argument(
            "--from-probes",
            type=ArgumentType.comma_separated_integers(minimum=1),
            metavar="PROBES",
            help="A comma-separated list of probe-ids you want to use in your "
                 "measurement. Example: --from-probes=1,2,34,157,10006"
        )
        origins.add_argument(
            "--from-measurement",
            type=ArgumentType.integer_range(minimum=1),
            metavar="MEASUREMENT_ID",
            help="A measurement id which you want to use as the basis for "
                 "probe selection in your new measurement.  This is a handy "
                 "way to re-create a measurement under conditions similar to "
                 "another measurement. Example: --from-measurement=1000192"
        )
        self.parser.add_argument(
            "--probes",
            type=ArgumentType.integer_range(minimum=1),
            default=conf["specification"]["source"]["requested"],
            help="The number of probes you want to use"
        )
        self.parser.add_argument(
            "--include-tag",
            type=ArgumentType.regex(r"^[a-z_\-]+$"),
            action="append",
            metavar="TAG",
            help="Include only probes that are marked with these tags. "
                 "Example: --include-tag=system-ipv6-works"
        )
        self.parser.add_argument(
            "--exclude-tag",
            type=ArgumentType.regex(r"^[a-z_\-]+$"),
            action="append",
            metavar="TAG",
            help="Exclude probes that are marked with these tags. "
                 "Example: --exclude-tag=system-ipv6-works"
        )

    def run(self):

        if self.arguments.dry_run:
            return self.dry_run()

        is_success, response = self.create()

        if not is_success:
            self._handle_api_error(response)  # Raises an exception

        pk = response["measurements"][0]
        url = "{0}/measurements/{1}/".format(conf["ripe-ncc"]["endpoint"], pk)

        self.ok(
            "Looking good!  Your measurement was created and details about "
            "it can be found here:\n\n  {0}".format(url)
        )

        if not self.arguments.no_report:
            self.stream(pk, url)

    def dry_run(self):

        print(colourise("\nDefinitions:\n{}".format("=" * 80), "bold"))

        for param, val in self._get_measurement_kwargs().items():
            print(colourise("{:<25} {}".format(param, val), "cyan"))

        print(colourise("\nSources:\n{}".format("=" * 80), "bold"))

        for param, val in self._get_source_kwargs().items():
            if param == "tags":
                print(colourise("tags\n  include{}{}\n  exclude{}{}\n".format(
                    " " * 17,
                    ", ".join(val["include"]),
                    " " * 17,
                    ", ".join(val["exclude"])
                ), "cyan"))
                continue
            print(colourise("{:<25} {}".format(param, val), "cyan"))

    def create(self):
        creation_class = self.CREATION_CLASSES[self._type]

        return AtlasCreateRequest(
            server=conf["ripe-ncc"]["endpoint"].replace("https://", ""),
            key=self.arguments.auth,
            measurements=[creation_class(**self._get_measurement_kwargs())],
            sources=[AtlasSource(**self._get_source_kwargs())],
            is_oneoff=self._is_oneoff
        ).create()

    def stream(self, pk, url):
        self.ok("Connecting to stream...")
        try:
            Stream(capture_limit=self.arguments.probes, timeout=300).stream(
                self.arguments.renderer, self._type, pk)
        except (KeyboardInterrupt, CaptureLimitExceeded):
            pass  # User said stop, so we fall through to the finally block.
        finally:
            self.ok("Disconnecting from stream\n\nYou can find details "
                    "about this measurement here:\n\n  {0}".format(url))

    def clean_target(self):

        if not self.arguments.target:
            raise RipeAtlasToolsException(
                "You must specify a target for that kind of measurement"
            )

        return self.arguments.target

    def clean_description(self):
        if self.arguments.description:
            return self.arguments.description
        if conf["specification"]["description"]:
            return conf["specification"]["description"]
        return "{} measurement to {}".format(
            self._type.capitalize(), self.arguments.target)

    def _get_measurement_kwargs(self):

        # This is kept apart from the r = {} because dns measurements don't
        # require a target attribute
        target = self.clean_target()

        r = {
            "af": self._get_af(),
            "description": self.clean_description(),
        }

        spec = conf["specification"]  # Shorter names are easier to read
        if self.arguments.interval or spec["times"]["interval"]:
            r["interval"] = self.arguments.interval
            self._is_oneoff = False
            self.arguments.no_report = True
        elif not spec["times"]["one-off"]:
            raise RipeAtlasToolsException(
                "Your configuration file appears to be setup to not create "
                "one-offs, but also offers no interval value.  Without one of "
                "these, a measurement cannot be created."
            )

        if target:
            r["target"] = target

        return r

    def _get_source_kwargs(self):

        r = conf["specification"]["source"]

        r["requested"] = self.arguments.probes
        if self.arguments.from_country:
            r["type"] = "country"
            r["value"] = self.arguments.from_country
        elif self.arguments.from_area:
            r["type"] = "area"
            r["value"] = self.arguments.from_area
        elif self.arguments.from_prefix:
            r["type"] = "prefix"
            r["value"] = self.arguments.from_prefix
        elif self.arguments.from_asn:
            r["type"] = "asn"
            r["value"] = self.arguments.from_asn
        elif self.arguments.from_probes:
            r["type"] = "probes"
            r["value"] = ",".join([str(_) for _ in self.arguments.from_probes])
        elif self.arguments.from_measurement:
            r["type"] = "msm"
            r["value"] = self.arguments.from_measurement

        r["tags"] = {
            "include": self.arguments.include_tag or [],
            "exclude": self.arguments.exclude_tag or []
        }

        af = "ipv{}".format(self._get_af())
        kind = self._type
        spec = conf["specification"]
        for clude in ("in", "ex"):
            clude += "clude"
            if not r["tags"][clude]:
                r["tags"][clude] += spec["tags"][af][kind][clude]
                r["tags"][clude] += spec["tags"][af]["all"][clude]

        return r

    def _get_af(self):
        """
        Returns the specified af, or a guessed one, or the configured one.  In
        that order.
        """
        if self.arguments.af:
            return self.arguments.af
        if self.arguments.target:
            if ":" in self.arguments.target:
                return 6
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", self.arguments.target):
                return 4
        return conf["specification"]["af"]

    @staticmethod
    def _handle_api_error(response):

        error_detail = response

        if isinstance(response, dict) and "detail" in response:
            error_detail = response["detail"]

        message = (
            "There was a problem communicating with the RIPE Atlas "
            "infrastructure.  The message given was:\n\n  {}"
        ).format(error_detail)

        raise RipeAtlasToolsException(message)


class PingMeasureCommand(Command):

    def add_arguments(self):

        Command.add_arguments(self)

        spec = conf["specification"]["types"]["ping"]

        specific = self.parser.add_argument_group("Ping-specific Options")
        specific.add_argument(
            "--packets",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["packets"],
            help="The number of packets sent"
        )
        specific.add_argument(
            "--size",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["size"],
            help="The size of packets sent"
        )
        specific.add_argument(
            "--packet-interval",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["packet-interval"],
        )

    def _get_measurement_kwargs(self):

        r = Command._get_measurement_kwargs(self)

        r["packets"] = self.arguments.packets
        r["packet_interval"] = self.arguments.packet_interval
        r["size"] = self.arguments.size

        return r


class TracerouteMeasureCommand(Command):

    def add_arguments(self):

        Command.add_arguments(self)

        spec = conf["specification"]["types"]["traceroute"]

        specific = self.parser.add_argument_group(
            "Traceroute-specific Options")
        specific.add_argument(
            "--packets",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["packets"],
            help="The number of packets sent"
        )
        specific.add_argument(
            "--size",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["size"],
            help="The size of packets sent"
        )
        specific.add_argument(
            "--protocol",
            type=str,
            choices=("ICMP", "UDP", "TCP"),
            default=spec["protocol"],
            help="The protocol used."
        )
        specific.add_argument(
            "--timeout",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["timeout"],
            help="The timeout per-packet"
        )
        specific.add_argument(
            "--dont-fragment",
            action="store_true",
            default=spec["dont-fragment"],
            help="Don't Fragment the packet"
        )
        specific.add_argument(
            "--paris",
            type=ArgumentType.integer_range(minimum=0, maximum=64),
            default=spec["paris"],
            help="Use Paris. Value must be between 0 and 64."
                 "If 0, a standard traceroute will be performed"
        )
        specific.add_argument(
            "--first-hop",
            type=ArgumentType.integer_range(minimum=1, maximum=255),
            default=spec["first-hop"],
            help="Value must be between 1 and 255"
        )
        specific.add_argument(
            "--max-hops",
            type=ArgumentType.integer_range(minimum=1, maximum=255),
            default=spec["max-hops"],
            help="Value must be between 1 and 255"
        )
        specific.add_argument(
            "--port",
            type=ArgumentType.integer_range(minimum=1, maximum=2**16),
            default=spec["port"],
            help="Destination port, valid for TCP only"
        )
        specific.add_argument(
            "--destination-option-size",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["destination-option-size"],
            help="IPv6 destination option header"
        )
        specific.add_argument(
            "--hop-by-hop-option-size",
            type=ArgumentType.integer_range(minimum=1),
            default=spec["hop-by-hop-option-size"],
            help=" IPv6 hop by hop option header"
        )

    def _get_measurement_kwargs(self):

        r = Command._get_measurement_kwargs(self)

        r["destination_option_size"] = self.arguments.destination_option_size
        r["dont_fragment"] = self.arguments.dont_fragment
        r["first_hop"] = self.arguments.first_hop
        r["hop_by_hop_option_size"] = self.arguments.hop_by_hop_option_size
        r["max_hops"] = self.arguments.max_hops
        r["packets"] = self.arguments.packets
        r["paris"] = self.arguments.paris
        r["port"] = self.arguments.port
        r["protocol"] = self.arguments.protocol
        r["size"] = self.arguments.size
        r["timeout"] = self.arguments.timeout

        return r


class DnsMeasureCommand(Command):

    def add_arguments(self):

        Command.add_arguments(self)

        specific = self.parser.add_argument_group("DNS-specific Options")
        specific.add_argument(
            "--protocol",
            type=str,
            choices=("UDP", "TCP"),
            default=conf["specification"]["types"]["dns"]["protocol"],
            help="The protocol used."
        )
        specific.add_argument(
            "--query-class",
            type=str,
            choices=("IN", "CHAOS"),
            default=conf["specification"]["types"]["dns"]["query-class"],
            help='The query class.  The default is "{}"'.format(
                conf["specification"]["types"]["dns"]["query-class"]
            )
        )
        specific.add_argument(
            "--query-type",
            type=str,
            choices=list(Message.ANSWER_CLASSES.keys()) + ["ANY"],  # The only ones we can parse
            default=conf["specification"]["types"]["dns"]["query-type"],
            help='The query type.  The default is "{}"'.format(
                conf["specification"]["types"]["dns"]["query-type"]
            )
        )
        specific.add_argument(
            "--query-argument",
            type=str,
            default=conf["specification"]["types"]["dns"]["query-argument"],
            help="The DNS label to query"
        )
        specific.add_argument(
            "--set-cd-bit",
            action="store_true",
            default=conf["specification"]["types"]["dns"]["set-cd-bit"],
            help="Set the DNSSEC Checking Disabled flag (RFC4035)"
        )
        specific.add_argument(
            "--set-do-bit",
            action="store_true",
            default=conf["specification"]["types"]["dns"]["set-do-bit"],
            help="Set the DNSSEC OK flag (RFC3225)"
        )
        specific.add_argument(
            "--set-nsid-bit",
            action="store_true",
            default=conf["specification"]["types"]["dns"]["set-nsid-bit"],
            help="Include an EDNS name server ID request with the query"
        )
        specific.add_argument(
            "--set-rd-bit",
            action="store_true",
            default=conf["specification"]["types"]["dns"]["set-rd-bit"],
            help="Set the Recursion Desired flag"
        )
        specific.add_argument(
            "--retry",
            type=ArgumentType.integer_range(minimum=1),
            default=conf["specification"]["types"]["dns"]["retry"],
            help="Number of times to retry"
        )
        specific.add_argument(
            "--udp-payload-size",
            type=ArgumentType.integer_range(minimum=1),
            default=conf["specification"]["types"]["dns"]["udp-payload-size"],
            help="May be any integer between 512 and 4096 inclusive"
        )

    def clean_target(self):
        """
        Targets aren't required for this type
        """
        return self.arguments.target

    def clean_description(self):
        if self.arguments.target:
            return Command.clean_description(self)
        return "DNS measurement for {}".format(self.arguments.query_argument)

    def _get_measurement_kwargs(self):

        r = Command._get_measurement_kwargs(self)

        for opt in ("class", "type", "argument"):
            if not getattr(self.arguments, "query_{0}".format(opt)):
                raise RipeAtlasToolsException(
                    "At a minimum, DNS measurements require a query argument.")

        r["query_class"] = self.arguments.query_class
        r["query_type"] = self.arguments.query_type
        r["query_argument"] = self.arguments.query_argument
        r["set_cd_bit"] = self.arguments.set_cd_bit
        r["set_do_bit"] = self.arguments.set_do_bit
        r["set_rd_bit"] = self.arguments.set_rd_bit
        r["set_nsid_bit"] = self.arguments.set_nsid_bit
        r["protocol"] = self.arguments.protocol
        r["retry"] = self.arguments.retry
        r["udp_payload_size"] = self.arguments.udp_payload_size
        r["use_probe_resolver"] = "target" not in r

        return r


class SslMeasureCommand(Command):
    pass


class NtpMeasureCommand(Command):

    def add_arguments(self):

        Command.add_arguments(self)

        specific = self.parser.add_argument_group("NTP-specific Options")
        specific.add_argument(
            "--timeout",
            type=ArgumentType.integer_range(minimum=1),
            default=conf["specification"]["types"]["ntp"]["timeout"],
            help="The timeout per-packet"
        )


class HttpMeasureCommand(Command):
    pass


class Factory(BaseFactory):

    TYPES = {
        "ping": PingMeasureCommand,
        "traceroute": TracerouteMeasureCommand,
        "dns": DnsMeasureCommand,
        "ssl": SslMeasureCommand,
        "ntp": NtpMeasureCommand,
    }

    def __init__(self):

        self.build_class = None
        if len(sys.argv) >= 2:
            self.build_class = self.TYPES.get(sys.argv[1].lower())

        if not self.build_class:
            raise RipeAtlasToolsException(
                "The measurement type you requested is invalid.  Please choose "
                "one of {}.".format(", ".join(self.TYPES.keys()))
            )

    def create(self, *args, **kwargs):
        return self.build_class(*args, **kwargs)
