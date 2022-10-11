"""
Place Support for OpenStreetMap Geocode sensors.

Original Author:  Jim Thompson
Subsequent Authors: Ian Richardson & Snuffy2

Description:
  Provides a sensor with a variable state consisting of reverse geocode (place) details for a linked device_tracker entity that provides GPS co-ordinates (ie owntracks, icloud)
  Allows you to specify a 'home_zone' for each device and calculates distance from home and direction of travel.
  Configuration Instructions are on GitHub.

GitHub: https://github.com/custom-components/places
"""

import copy
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

import homeassistant.helpers.config_validation as cv
import requests
import voluptuous as vol
from homeassistant import config_entries, core
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_GPS_ACCURACY,
    CONF_API_KEY,
    CONF_ICON,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    CONF_UNIQUE_ID,
    CONF_ZONE,
    EVENT_HOMEASSISTANT_START,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import Throttle, slugify
from homeassistant.util.location import distance
from urllib3.exceptions import NewConnectionError

from .const import (
    ATTR_CITY,
    ATTR_COUNTRY,
    ATTR_COUNTY,
    ATTR_DEVICETRACKER_ID,
    ATTR_DEVICETRACKER_ZONE,
    ATTR_DEVICETRACKER_ZONE_NAME,
    ATTR_DIRECTION_OF_TRAVEL,
    ATTR_DISPLAY_OPTIONS,
    ATTR_DISTANCE_FROM_HOME_KM,
    ATTR_DISTANCE_FROM_HOME_M,
    ATTR_DISTANCE_FROM_HOME_MI,
    ATTR_DISTANCE_TRAVELED_M,
    ATTR_DISTANCE_TRAVELED_MI,
    ATTR_FORMATTED_ADDRESS,
    ATTR_FORMATTED_PLACE,
    ATTR_HOME_LATITUDE,
    ATTR_HOME_LOCATION,
    ATTR_HOME_LONGITUDE,
    ATTR_INITIAL_UPDATE,
    ATTR_IS_DRIVING,
    ATTR_JSON_FILENAME,
    ATTR_LAST_CHANGED,
    ATTR_LAST_PLACE_NAME,
    ATTR_LAST_UPDATED,
    ATTR_LATITUDE,
    ATTR_LATITUDE_OLD,
    ATTR_LOCATION_CURRENT,
    ATTR_LOCATION_PREVIOUS,
    ATTR_LONGITUDE,
    ATTR_LONGITUDE_OLD,
    ATTR_MAP_LINK,
    ATTR_NATIVE_VALUE,
    ATTR_OPTIONS,
    ATTR_OSM_DETAILS_DICT,
    ATTR_OSM_DICT,
    ATTR_OSM_ID,
    ATTR_OSM_TYPE,
    ATTR_PICTURE,
    ATTR_PLACE_CATEGORY,
    ATTR_PLACE_NAME,
    ATTR_PLACE_NEIGHBOURHOOD,
    ATTR_PLACE_TYPE,
    ATTR_POSTAL_CODE,
    ATTR_POSTAL_TOWN,
    ATTR_PREVIOUS_STATE,
    ATTR_REGION,
    ATTR_STATE_ABBR,
    ATTR_STREET,
    ATTR_STREET_NUMBER,
    ATTR_STREET_REF,
    ATTR_UPDATES_SKIPPED,
    ATTR_WIKIDATA_DICT,
    ATTR_WIKIDATA_ID,
    CONF_DEVICETRACKER_ID,
    CONF_EXTENDED_ATTR,
    CONF_HOME_ZONE,
    CONF_LANGUAGE,
    CONF_MAP_PROVIDER,
    CONF_MAP_ZOOM,
    CONF_OPTIONS,
    CONF_SHOW_TIME,
    CONF_YAML_HASH,
    CONFIG_ATTRIBUTES_LIST,
    DEFAULT_EXTENDED_ATTR,
    DEFAULT_HOME_ZONE,
    DEFAULT_MAP_PROVIDER,
    DEFAULT_MAP_ZOOM,
    DEFAULT_OPTION,
    DEFAULT_SHOW_TIME,
    DOMAIN,
    EVENT_ATTRIBUTE_LIST,
    EXTENDED_ATTRIBUTE_LIST,
    EXTRA_STATE_ATTRIBUTE_LIST,
    HOME_LOCATION_DOMAINS,
    JSON_ATTRIBUTE_LIST,
    JSON_IGNORE_ATTRIBUTE_LIST,
    PLACE_NAME_DUPLICATE_LIST,
    RESET_ATTRIBUTE_LIST,
    TRACKING_DOMAINS,
    TRACKING_DOMAINS_NEED_LATLONG,
)

_LOGGER = logging.getLogger(__name__)
try:
    use_issue_reg = True
    from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
except Exception as e:
    _LOGGER.debug(
        "Unknown Exception trying to import issue_registry. Is HA version <2022.9?: "
        + str(e)
    )
    use_issue_reg = False

THROTTLE_INTERVAL = timedelta(seconds=600)
SCAN_INTERVAL = timedelta(seconds=30)
PLACES_JSON_FOLDER = os.path.join("custom_components", DOMAIN, "json_sensors")
try:
    os.makedirs(PLACES_JSON_FOLDER, exist_ok=True)
except OSError as e:
    _LOGGER.warning("OSError creating folder for JSON sensor files: " + str(e))
except Exception as e:
    _LOGGER.warning(
        "Unknown Exception creating folder for JSON sensor files: " + str(e)
    )
ICON = "mdi:map-search-outline"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_DEVICETRACKER_ID): cv.string,
        vol.Optional(CONF_API_KEY): cv.string,
        vol.Optional(CONF_OPTIONS, default=DEFAULT_OPTION): cv.string,
        vol.Optional(CONF_HOME_ZONE, default=DEFAULT_HOME_ZONE): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_MAP_PROVIDER, default=DEFAULT_MAP_PROVIDER): cv.string,
        vol.Optional(CONF_MAP_ZOOM, default=DEFAULT_MAP_ZOOM): cv.positive_int,
        vol.Optional(CONF_LANGUAGE): cv.string,
        vol.Optional(CONF_EXTENDED_ATTR, default=DEFAULT_EXTENDED_ATTR): cv.boolean,
        vol.Optional(CONF_SHOW_TIME, default=DEFAULT_SHOW_TIME): cv.boolean,
    }
)


async def async_setup_platform(
    hass: core.HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType = None,
) -> None:
    """Set up places sensor from YAML."""

    @core.callback
    def schedule_import(_):
        """Schedule delayed import after HA is fully started."""
        _LOGGER.debug("[YAML Import] Awaiting HA Startup before importing")
        async_call_later(hass, 10, do_import)

    @core.callback
    def do_import(_):
        """Process YAML import."""
        _LOGGER.debug("[YAML Import] HA Started, proceeding")
        if validate_import():
            _LOGGER.warning(
                "[YAML Import] New YAML sensor, importing: "
                + str(import_config.get(CONF_NAME))
            )

            if use_issue_reg and import_config is not None:
                async_create_issue(
                    hass,
                    DOMAIN,
                    "deprecated_yaml",
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="deprecated_yaml",
                )

            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": config_entries.SOURCE_IMPORT},
                    data=import_config,
                )
            )
        # else:
        #    _LOGGER.debug("[YAML Import] Failed validation, not importing")

    @core.callback
    def validate_import():
        if CONF_DEVICETRACKER_ID not in import_config:
            # device_tracker not defined in config
            ERROR = "[YAML Validate] Not importing: devicetracker_id not defined in the YAML places sensor definition"
            _LOGGER.error(ERROR)
            return False
        elif import_config.get(CONF_DEVICETRACKER_ID) is None:
            # device_tracker not defined in config
            ERROR = "[YAML Validate] Not importing: devicetracker_id not defined in the YAML places sensor definition"
            _LOGGER.error(ERROR)
            return False
        _LOGGER.debug(
            "[YAML Validate] devicetracker_id: "
            + str(import_config.get(CONF_DEVICETRACKER_ID))
        )
        if (
            import_config.get(CONF_DEVICETRACKER_ID).split(".")[0]
            not in TRACKING_DOMAINS
        ):
            # entity isn't in supported type
            ERROR = (
                "[YAML Validate] Not importing: devicetracker_id: "
                + str(import_config.get(CONF_DEVICETRACKER_ID))
                + " is not one of the supported types: "
                + str(list(TRACKING_DOMAINS))
            )
            _LOGGER.error(ERROR)
            return False
        elif not hass.states.get(import_config.get(CONF_DEVICETRACKER_ID)):
            # entity doesn't exist
            ERROR = (
                "[YAML Validate] Not importing: devicetracker_id: "
                + str(import_config.get(CONF_DEVICETRACKER_ID))
                + " doesn't exist"
            )
            _LOGGER.error(ERROR)
            return False

        if import_config.get(CONF_DEVICETRACKER_ID).split(".")[
            0
        ] in TRACKING_DOMAINS_NEED_LATLONG and not (
            CONF_LATITUDE
            in hass.states.get(import_config.get(CONF_DEVICETRACKER_ID)).attributes
            and CONF_LONGITUDE
            in hass.states.get(import_config.get(CONF_DEVICETRACKER_ID)).attributes
        ):
            _LOGGER.debug(
                "[YAML Validate] devicetracker_id: "
                + str(import_config.get(CONF_DEVICETRACKER_ID))
                + " - "
                + CONF_LATITUDE
                + "= "
                + str(
                    hass.states.get(
                        import_config.get(CONF_DEVICETRACKER_ID)
                    ).attributes.get(CONF_LATITUDE)
                )
            )
            _LOGGER.debug(
                "[YAML Validate] devicetracker_id: "
                + str(import_config.get(CONF_DEVICETRACKER_ID))
                + " - "
                + CONF_LONGITUDE
                + "= "
                + str(
                    hass.states.get(
                        import_config.get(CONF_DEVICETRACKER_ID)
                    ).attributes.get(CONF_LONGITUDE)
                )
            )
            ERROR = (
                "[YAML Validate] Not importing: devicetracker_id: "
                + import_config.get(CONF_DEVICETRACKER_ID)
                + " doesnt have latitude/longitude as attributes"
            )
            _LOGGER.error(ERROR)
            return False

        if CONF_HOME_ZONE in import_config:
            if import_config.get(CONF_HOME_ZONE) is None:
                # home zone not defined in config
                ERROR = "[YAML Validate] Not importing: home_zone is blank in the YAML places sensor definition"
                _LOGGER.error(ERROR)
                return False
            _LOGGER.debug(
                "[YAML Validate] home_zone: " + str(import_config.get(CONF_HOME_ZONE))
            )

            if (
                import_config.get(CONF_HOME_ZONE).split(".")[0]
                not in HOME_LOCATION_DOMAINS
            ):
                # entity isn't in supported type
                ERROR = (
                    "[YAML Validate] Not importing: home_zone: "
                    + str(import_config.get(CONF_HOME_ZONE))
                    + " is not one of the supported types: "
                    + str(list(HOME_LOCATION_DOMAINS))
                )
                _LOGGER.error(ERROR)
                return False
            elif not hass.states.get(import_config.get(CONF_HOME_ZONE)):
                # entity doesn't exist
                ERROR = (
                    "[YAML Validate] Not importing: home_zone: "
                    + str(import_config.get(CONF_HOME_ZONE))
                    + " doesn't exist"
                )
                _LOGGER.error(ERROR)
                return False

        # Generate pseudo-unique id using MD5 and store in config to try to prevent reimporting already imported yaml sensors.
        string_to_hash = (
            import_config.get(CONF_NAME)
            + import_config.get(CONF_DEVICETRACKER_ID)
            + import_config.get(CONF_HOME_ZONE)
        )
        # _LOGGER.debug(
        #    "[YAML Validate] string_to_hash: " + str(string_to_hash)
        # )
        yaml_hash_object = hashlib.md5(string_to_hash.encode())
        yaml_hash = yaml_hash_object.hexdigest()

        import_config.setdefault(CONF_YAML_HASH, yaml_hash)
        # _LOGGER.debug("[YAML Validate] final import_config: " + str(import_config))

        all_yaml_hashes = []
        if (
            DOMAIN in hass.data
            and hass.data.get(DOMAIN) is not None
            and hass.data.get(DOMAIN).values() is not None
        ):
            for m in list(hass.data.get(DOMAIN).values()):
                if CONF_YAML_HASH in m:
                    all_yaml_hashes.append(m.get(CONF_YAML_HASH))

        # _LOGGER.debug(
        #    "[YAML Validate] YAML hash: " + str(import_config.get(CONF_YAML_HASH))
        # )
        # _LOGGER.debug(
        #    "[YAML Validate] All existing YAML hashes: " + str(all_yaml_hashes)
        # )
        if import_config.get(CONF_YAML_HASH) not in all_yaml_hashes:
            return True
        else:
            _LOGGER.info(
                "[YAML Validate] YAML sensor already imported, ignoring: "
                + str(import_config.get(CONF_NAME))
            )
            return False

    import_config = dict(config)
    _LOGGER.debug("[YAML Import] initial import_config: " + str(import_config))
    import_config.pop(CONF_PLATFORM, None)
    import_config.pop(CONF_SCAN_INTERVAL, None)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, schedule_import)


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
) -> None:
    """Setup the sensor platform with a config_entry (config_flow)."""

    # _LOGGER.debug("[aync_setup_entity] all entities: " +
    #              str(hass.data.get(DOMAIN)))

    config = hass.data.get(DOMAIN).get(config_entry.entry_id)
    unique_id = config_entry.entry_id
    name = config.get(CONF_NAME)
    # _LOGGER.debug("[async_setup_entry] name: " + str(name))
    # _LOGGER.debug("[async_setup_entry] unique_id: " + str(unique_id))
    # _LOGGER.debug("[async_setup_entry] config: " + str(config))

    async_add_entities(
        [Places(hass, config, config_entry, name, unique_id)], update_before_add=True
    )


class Places(SensorEntity):
    """Representation of a Places Sensor."""

    def __init__(self, hass, config, config_entry, name, unique_id):
        """Initialize the sensor."""
        self._attr_should_poll = True
        _LOGGER.info("(" + str(name) + ") [Init] Places sensor: " + str(name))

        self._internal_attr = {}
        self.set_attr(ATTR_INITIAL_UPDATE, True)
        self._config = config
        self._config_entry = config_entry
        self._hass = hass
        self.set_attr(CONF_NAME, name)
        self._attr_name = name
        self.set_attr(CONF_UNIQUE_ID, unique_id)
        self._attr_unique_id = unique_id
        self.set_attr(CONF_ICON, ICON)
        self._attr_icon = ICON
        self.set_attr(CONF_API_KEY, config.get(CONF_API_KEY))
        self.set_attr(
            CONF_OPTIONS, config.setdefault(CONF_OPTIONS, DEFAULT_OPTION).lower()
        )
        self.set_attr(CONF_DEVICETRACKER_ID, config.get(CONF_DEVICETRACKER_ID).lower())
        # Consider reconciling this in the future
        self.set_attr(ATTR_DEVICETRACKER_ID, config.get(CONF_DEVICETRACKER_ID).lower())
        self.set_attr(
            CONF_HOME_ZONE, config.setdefault(CONF_HOME_ZONE, DEFAULT_HOME_ZONE).lower()
        )
        self.set_attr(
            CONF_MAP_PROVIDER,
            config.setdefault(CONF_MAP_PROVIDER, DEFAULT_MAP_PROVIDER).lower(),
        )
        self.set_attr(
            CONF_MAP_ZOOM, int(config.setdefault(CONF_MAP_ZOOM, DEFAULT_MAP_ZOOM))
        )
        self.set_attr(CONF_LANGUAGE, config.get(CONF_LANGUAGE))

        if not self.is_attr_blank(CONF_LANGUAGE):
            self.set_attr(
                CONF_LANGUAGE, self.get_attr(CONF_LANGUAGE).replace(" ", "").strip()
            )

        self.set_attr(
            CONF_EXTENDED_ATTR,
            config.setdefault(CONF_EXTENDED_ATTR, DEFAULT_EXTENDED_ATTR),
        )

        self.set_attr(
            CONF_SHOW_TIME, config.setdefault(CONF_SHOW_TIME, DEFAULT_SHOW_TIME)
        )
        self.set_attr(
            ATTR_JSON_FILENAME,
            (DOMAIN + "-" + slugify(str(self.get_attr(CONF_UNIQUE_ID))) + ".json"),
        )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") [Init] JSON Filename: "
            + str(self.get_attr(ATTR_JSON_FILENAME))
        )

        self._attr_native_value = None  # Represents the state in SensorEntity
        self.clear_attr(ATTR_NATIVE_VALUE)

        if (
            not self.is_attr_blank(CONF_HOME_ZONE)
            and CONF_LATITUDE
            in hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes
            and hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes.get(
                CONF_LATITUDE
            )
            is not None
            and self.is_float(
                hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes.get(
                    CONF_LATITUDE
                )
            )
        ):
            self.set_attr(
                ATTR_HOME_LATITUDE,
                str(
                    hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes.get(
                        CONF_LATITUDE
                    )
                ),
            )
        if (
            not self.is_attr_blank(CONF_HOME_ZONE)
            and CONF_LONGITUDE
            in hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes
            and hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes.get(
                CONF_LONGITUDE
            )
            is not None
            and self.is_float(
                hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes.get(
                    CONF_LONGITUDE
                )
            )
        ):
            self.set_attr(
                ATTR_HOME_LONGITUDE,
                str(
                    hass.states.get(self.get_attr(CONF_HOME_ZONE)).attributes.get(
                        CONF_LONGITUDE
                    )
                ),
            )

        self._attr_entity_picture = (
            hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes.get(
                ATTR_PICTURE
            )
            if hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID))
            else None
        )

        self.set_attr(ATTR_UPDATES_SKIPPED, 0)

        sensor_attributes = self.get_dict_from_json_file()
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") [Init] Sensor Attributes to Import: "
        #    + str(sensor_attributes)
        # )
        self.import_attributes_from_json(sensor_attributes)
        ##
        # For debugging:
        # sensor_attributes = {}
        # sensor_attributes.update({CONF_NAME: self.get_attr(CONF_NAME)})
        # sensor_attributes.update({ATTR_NATIVE_VALUE: self.get_attr(ATTR_NATIVE_VALUE)})
        # sensor_attributes.update(self.extra_state_attributes)
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") [Init] Sensor Attributes Imported: "
        #    + str(sensor_attributes)
        # )
        ##
        if not self.get_attr(ATTR_INITIAL_UPDATE):
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") [Init] Sensor Attributes Imported from JSON file"
            )
        self.cleanup_attributes()
        _LOGGER.info(
            "("
            + self.get_attr(CONF_NAME)
            + ") [Init] DeviceTracker Entity ID: "
            + self.get_attr(CONF_DEVICETRACKER_ID)
        )

    def get_dict_from_json_file(self):
        sensor_attributes = {}
        try:
            with open(
                os.path.join(PLACES_JSON_FOLDER, self.get_attr(ATTR_JSON_FILENAME)),
                "r",
            ) as jsonfile:
                sensor_attributes = json.load(jsonfile)
        except OSError as e:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") [Init] No JSON file to import ("
                + str(self.get_attr(ATTR_JSON_FILENAME))
                + "): "
                + str(e)
            )
            return {}
        except Exception as e:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") [Init] Unknown Exception importing JSON file ("
                + str(self.get_attr(ATTR_JSON_FILENAME))
                + "): "
                + str(e)
            )
            return {}
        return sensor_attributes

    async def async_added_to_hass(self) -> None:
        """Added to hass."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                self.get_attr(CONF_DEVICETRACKER_ID),
                self.tsc_update,
            )
        )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") [Init] Subscribed to DeviceTracker state change events"
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        try:
            os.remove(
                os.path.join(PLACES_JSON_FOLDER, self.get_attr(ATTR_JSON_FILENAME))
            )
        except OSError as e:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") OSError removing JSON sensor file ("
                + str(self.get_attr(ATTR_JSON_FILENAME))
                + "): "
                + str(e)
            )
        except Exception as e:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Unknown Exception removing JSON sensor file ("
                + str(self.get_attr(ATTR_JSON_FILENAME))
                + "): "
                + str(e)
            )
        else:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") JSON sensor file removed: "
                + str(self.get_attr(ATTR_JSON_FILENAME))
            )

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return_attr = {}
        self.cleanup_attributes()
        for attr in EXTRA_STATE_ATTRIBUTE_LIST:
            if self.get_attr(attr):
                return_attr.update({attr: self.get_attr(attr)})

        if self.get_attr(CONF_EXTENDED_ATTR):
            for attr in EXTENDED_ATTRIBUTE_LIST:
                if self.get_attr(attr):
                    return_attr.update({attr: self.get_attr(attr)})
        # _LOGGER.debug("(" + self.get_attr(CONF_NAME) + ") Extra State Attributes: " + str(return_attr))
        return return_attr

    def import_attributes_from_json(self, json_attr=None):
        """Import the JSON state attributes. Takes a Dictionary as input."""
        if json_attr is None or not isinstance(json_attr, dict) or not json_attr:
            return

        self.set_attr(ATTR_INITIAL_UPDATE, False)
        for attr in JSON_ATTRIBUTE_LIST:
            if attr in json_attr:
                self.set_attr(attr, json_attr.pop(attr, None))
        if not self.is_attr_blank(ATTR_NATIVE_VALUE):
            self._attr_native_value = self.get_attr(ATTR_NATIVE_VALUE)

        # Remove attributes that are part of the Config and are explicitly not imported from JSON
        for attr in CONFIG_ATTRIBUTES_LIST + JSON_IGNORE_ATTRIBUTE_LIST:
            if attr in json_attr:
                json_attr.pop(attr, None)
        if json_attr is not None and json_attr:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") [import_attributes] Attributes not imported: "
                + str(json_attr)
            )

    def get_attr(self, attr, default=None):
        if attr is None or (default is None and self.is_attr_blank(attr)):
            return None
        else:
            return self._internal_attr.get(attr, default)

    def set_attr(self, attr, value=None):
        if attr is not None:
            self._internal_attr.update({attr: value})

    def clear_attr(self, attr):
        self._internal_attr.pop(attr, None)

    def is_devicetracker_set(self):

        if (
            not self.is_attr_blank(CONF_DEVICETRACKER_ID)
            and hasattr(
                self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)),
                "attributes",
            )
            and CONF_LATITUDE
            in self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes
            and CONF_LONGITUDE
            in self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes
            and self._hass.states.get(
                self.get_attr(CONF_DEVICETRACKER_ID)
            ).attributes.get(CONF_LATITUDE)
            is not None
            and self._hass.states.get(
                self.get_attr(CONF_DEVICETRACKER_ID)
            ).attributes.get(CONF_LONGITUDE)
            is not None
            and self.is_float(
                self._hass.states.get(
                    self.get_attr(CONF_DEVICETRACKER_ID)
                ).attributes.get(CONF_LATITUDE)
            )
            and self.is_float(
                self._hass.states.get(
                    self.get_attr(CONF_DEVICETRACKER_ID)
                ).attributes.get(CONF_LONGITUDE)
            )
        ):
            # _LOGGER.debug(
            #    "(" + self.get_attr(CONF_NAME) +
            #    ") [is_devicetracker_set] Devicetracker is set"
            # )
            return True
        else:
            # _LOGGER.debug(
            #    "(" + self.get_attr(CONF_NAME) +
            #    ") [is_devicetracker_set] Devicetracker is not set"
            # )
            return False

    def tsc_update(self, tscarg=None):
        """Call the do_update function based on the TSC (track state change) event"""
        if self.is_devicetracker_set():
            # _LOGGER.debug(
            #    "("
            #    + self.get_attr(CONF_NAME)
            #    + ") [TSC Update] Running Update - Devicetracker is set"
            # )
            self.do_update("Track State Change")
        # else:
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") [TSC Update] Not Running Update - Devicetracker is not set"
        # )

    @Throttle(THROTTLE_INTERVAL)
    async def async_update(self):
        """Call the do_update function based on scan interval and throttle"""
        if self.is_devicetracker_set():
            # _LOGGER.debug(
            #    "("
            #    + self.get_attr(CONF_NAME)
            #    + ") [Async Update] Running Update - Devicetracker is set"
            # )
            await self._hass.async_add_executor_job(self.do_update, "Scan Interval")
        # else:
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") [Async Update] Not Running Update - Devicetracker is not set"
        # )

    def haversine(self, lon1, lat1, lon2, lat2):
        """
        Calculate the great circle distance between two points
        on the earth (specified in decimal degrees)
        """
        # convert decimal degrees to radians
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])

        # haversine formula
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        r = 6371  # Radius of earth in kilometers. Use 3956 for miles
        return c * r

    def is_float(self, value):
        if value is not None:
            try:
                float(value)
                return True
            except ValueError:
                return False
        else:
            return False

    def in_zone(self):
        if not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE):
            if (
                "stationary" in self.get_attr(ATTR_DEVICETRACKER_ZONE).lower()
                or self.get_attr(ATTR_DEVICETRACKER_ZONE).lower() == "away"
                or self.get_attr(ATTR_DEVICETRACKER_ZONE).lower() == "not_home"
                or self.get_attr(ATTR_DEVICETRACKER_ZONE).lower() == "notset"
            ):
                return False
            else:
                return True
        else:
            return False

    def is_attr_blank(self, attr):
        if self._internal_attr.get(attr) or self._internal_attr.get(attr) == 0:
            return False
        else:
            return True

    def cleanup_attributes(self):
        for attr in list(self._internal_attr):
            if self.is_attr_blank(attr):
                self.clear_attr(attr)

    def check_for_updated_entity_name(self):
        if hasattr(self, "entity_id") and self.entity_id is not None:
            # _LOGGER.debug("(" + self.get_attr(CONF_NAME) + ") Entity ID: " + str(self.entity_id))
            if (
                self._hass.states.get(str(self.entity_id)) is not None
                and self._hass.states.get(str(self.entity_id)).attributes.get(
                    ATTR_FRIENDLY_NAME
                )
                is not None
                and self.get_attr(CONF_NAME)
                != self._hass.states.get(str(self.entity_id)).attributes.get(
                    ATTR_FRIENDLY_NAME
                )
            ):
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Sensor Name Changed. Updating Name to: "
                    + str(
                        self._hass.states.get(str(self.entity_id)).attributes.get(
                            ATTR_FRIENDLY_NAME
                        )
                    )
                )
                self.set_attr(
                    CONF_NAME,
                    self._hass.states.get(str(self.entity_id)).attributes.get(
                        ATTR_FRIENDLY_NAME
                    ),
                )
                self._config.update({CONF_NAME: self.get_attr(CONF_NAME)})
                self.set_attr(CONF_NAME, self.get_attr(CONF_NAME))
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Updated Config Name: "
                    + str(self._config.get(CONF_NAME, None))
                )
                self._hass.config_entries.async_update_entry(
                    self._config_entry,
                    data=self._config,
                    options=self._config_entry.options,
                )
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Updated ConfigEntry Name: "
                    + str(self._config_entry.data.get(CONF_NAME))
                )

    def get_zone_details(self):
        self.set_attr(
            ATTR_DEVICETRACKER_ZONE,
            self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).state,
        )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") DeviceTracker Zone: "
            + str(self.get_attr(ATTR_DEVICETRACKER_ZONE))
        )

        devicetracker_zone_name_state = None
        devicetracker_zone_id = self._hass.states.get(
            self.get_attr(CONF_DEVICETRACKER_ID)
        ).attributes.get(CONF_ZONE)
        if devicetracker_zone_id is not None:
            devicetracker_zone_id = str(CONF_ZONE) + "." + str(devicetracker_zone_id)
            devicetracker_zone_name_state = self._hass.states.get(devicetracker_zone_id)
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") DeviceTracker Zone ID: "
        #    + str(devicetracker_zone_id)
        # )
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") DeviceTracker Zone Name State: "
        #    + str(devicetracker_zone_name_state)
        # )
        if devicetracker_zone_name_state is not None:
            self.set_attr(
                ATTR_DEVICETRACKER_ZONE_NAME, devicetracker_zone_name_state.name
            )
        else:
            self.set_attr(
                ATTR_DEVICETRACKER_ZONE_NAME, self.get_attr(ATTR_DEVICETRACKER_ZONE)
            )
        if not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE_NAME) and self.get_attr(
            ATTR_DEVICETRACKER_ZONE_NAME
        ).lower() == self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME):
            self.set_attr(
                ATTR_DEVICETRACKER_ZONE_NAME,
                self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME).title(),
            )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") DeviceTracker Zone Name: "
            + str(self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME))
        )

    def determine_if_update_needed(self):
        proceed_with_update = True
        if (
            not self.is_attr_blank(ATTR_GPS_ACCURACY)
            and self.get_attr(ATTR_GPS_ACCURACY) == 0
        ):
            proceed_with_update = False
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") GPS Accuracy is 0, not performing update"
            )
        elif self.get_attr(ATTR_INITIAL_UPDATE):
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Performing Initial Update for user..."
            )
            proceed_with_update = True
        elif self.get_attr(ATTR_LOCATION_CURRENT) == self.get_attr(
            ATTR_LOCATION_PREVIOUS
        ):
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Not performing update because coordinates are identical"
            )
            proceed_with_update = False
        elif (
            int(self.get_attr(ATTR_DISTANCE_TRAVELED_M)) > 0
            and self.get_attr(ATTR_UPDATES_SKIPPED) > 3
        ):
            proceed_with_update = True
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Allowing update after 3 skips even with distance traveled < 10m"
            )
        elif int(self.get_attr(ATTR_DISTANCE_TRAVELED_M)) < 10:
            self.set_attr(ATTR_UPDATES_SKIPPED, self.get_attr(ATTR_UPDATES_SKIPPED) + 1)
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Not performing update because location changed "
                + str(round(self.get_attr(ATTR_DISTANCE_TRAVELED_M), 1))
                + " < 10m  ("
                + str(self.get_attr(ATTR_UPDATES_SKIPPED))
                + ")"
            )
            proceed_with_update = False
        return proceed_with_update

    def get_dict_from_url(self, url, name):
        get_dict = {}
        _LOGGER.info(
            "(" + self.get_attr(CONF_NAME) + ") Requesting data for " + str(name)
        )
        _LOGGER.debug(
            "(" + self.get_attr(CONF_NAME) + ") " + str(name) + " URL: " + str(url)
        )
        try:
            get_response = requests.get(url)
        except requests.exceptions.Timeout as e:
            get_response = None
            _LOGGER.warning(
                "("
                + self.get_attr(CONF_NAME)
                + ") Timeout connecting to "
                + str(name)
                + " [Error: "
                + str(e)
                + "]: "
                + str(url)
            )
            return {}
        except OSError as e:
            # Includes error code 101, network unreachable
            get_response = None
            _LOGGER.warning(
                "("
                + self.get_attr(CONF_NAME)
                + ") Network unreachable error when connecting to "
                + str(name)
                + " ["
                + str(e)
                + "]: "
                + str(url)
            )
            return {}
        except NewConnectionError as e:
            get_response = None
            _LOGGER.warning(
                "("
                + self.get_attr(CONF_NAME)
                + ") Connection Error connecting to "
                + str(name)
                + " [Error: "
                + str(e)
                + "]: "
                + str(url)
            )
            return {}
        except Exception as e:
            get_response = None
            _LOGGER.warning(
                "("
                + self.get_attr(CONF_NAME)
                + ") Unknown Exception connecting to "
                + str(name)
                + " [Error: "
                + str(e)
                + "]: "
                + str(url)
            )
            return {}

        get_json_input = {}
        if get_response is not None and get_response:
            get_json_input = get_response.text
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") "
                + str(name)
                + " Response: "
                + get_json_input
            )

        if get_json_input is not None and get_json_input:
            try:
                get_dict = json.loads(get_json_input)
            except json.decoder.JSONDecodeError as e:
                _LOGGER.warning(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") JSON Decode Error with "
                    + str(name)
                    + " info [Error: "
                    + str(e)
                    + "]: "
                    + str(get_json_input)
                )
                return {}
        if "error_message" in get_dict:
            _LOGGER.warning(
                "("
                + self.get_attr(CONF_NAME)
                + ") An error occurred contacting the web service for "
                + str(name)
                + ": "
                + str(get_dict.get("error_message"))
            )
            return {}
        return get_dict

    def get_map_link(self):

        if self.get_attr(CONF_MAP_PROVIDER) == "google":
            self.set_attr(
                ATTR_MAP_LINK,
                (
                    "https://maps.google.com/?q="
                    + str(self.get_attr(ATTR_LOCATION_CURRENT))
                    + "&ll="
                    + str(self.get_attr(ATTR_LOCATION_CURRENT))
                    + "&z="
                    + str(self.get_attr(CONF_MAP_ZOOM))
                ),
            )
        elif self.get_attr(CONF_MAP_PROVIDER) == "osm":
            self.set_attr(
                ATTR_MAP_LINK,
                (
                    "https://www.openstreetmap.org/?mlat="
                    + str(self.get_attr(ATTR_LATITUDE))
                    + "&mlon="
                    + str(self.get_attr(ATTR_LONGITUDE))
                    + "#map="
                    + str(self.get_attr(CONF_MAP_ZOOM))
                    + "/"
                    + str(self.get_attr(ATTR_LATITUDE))[:8]
                    + "/"
                    + str(self.get_attr(ATTR_LONGITUDE))[:9]
                ),
            )
        else:
            self.set_attr(
                ATTR_MAP_LINK,
                (
                    "https://maps.apple.com/maps/?q="
                    + str(self.get_attr(ATTR_LOCATION_CURRENT))
                    + "&z="
                    + str(self.get_attr(CONF_MAP_ZOOM))
                ),
            )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Map Link Type: "
            + str(self.get_attr(CONF_MAP_PROVIDER))
        )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Map Link URL: "
            + str(self.get_attr(ATTR_MAP_LINK))
        )

    def get_gps_accuracy(self):
        if (
            self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID))
            and self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes
            and ATTR_GPS_ACCURACY
            in self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes
            and self._hass.states.get(
                self.get_attr(CONF_DEVICETRACKER_ID)
            ).attributes.get(ATTR_GPS_ACCURACY)
            is not None
            and self.is_float(
                self._hass.states.get(
                    self.get_attr(CONF_DEVICETRACKER_ID)
                ).attributes.get(ATTR_GPS_ACCURACY)
            )
        ):
            self.set_attr(
                ATTR_GPS_ACCURACY,
                float(
                    self._hass.states.get(
                        self.get_attr(CONF_DEVICETRACKER_ID)
                    ).attributes.get(ATTR_GPS_ACCURACY)
                ),
            )
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") GPS Accuracy: "
                + str(round(self.get_attr(ATTR_GPS_ACCURACY), 3))
            )
        else:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") GPS Accuracy attribute not found in: "
                + str(self.get_attr(CONF_DEVICETRACKER_ID))
            )

    def get_driving_status(self):
        isDriving = False
        if not self.in_zone():
            if self.get_attr(ATTR_DIRECTION_OF_TRAVEL) != "stationary" and (
                self.get_attr(ATTR_PLACE_CATEGORY) == "highway"
                or self.get_attr(ATTR_PLACE_TYPE) == "motorway"
            ):
                isDriving = True
        self.set_attr(ATTR_IS_DRIVING, isDriving)

    def parse_osm_dict(self):
        if "type" in self.get_attr(ATTR_OSM_DICT):
            self.set_attr(ATTR_PLACE_TYPE, self.get_attr(ATTR_OSM_DICT).get("type"))
            if self.get_attr(ATTR_PLACE_TYPE) == "yes":
                self.set_attr(
                    ATTR_PLACE_TYPE,
                    self.get_attr(ATTR_OSM_DICT).get("addresstype"),
                )
            if self.get_attr(ATTR_PLACE_TYPE) in self.get_attr(ATTR_OSM_DICT).get(
                "address"
            ):
                self.set_attr(
                    ATTR_PLACE_NAME,
                    self.get_attr(ATTR_OSM_DICT)
                    .get("address")
                    .get(self.get_attr(ATTR_PLACE_TYPE)),
                )
        if "category" in self.get_attr(ATTR_OSM_DICT):
            self.set_attr(
                ATTR_PLACE_CATEGORY,
                self.get_attr(ATTR_OSM_DICT).get("category"),
            )
            if self.get_attr(ATTR_PLACE_CATEGORY) in self.get_attr(ATTR_OSM_DICT).get(
                "address"
            ):
                self.set_attr(
                    ATTR_PLACE_NAME,
                    self.get_attr(ATTR_OSM_DICT)
                    .get("address")
                    .get(self.get_attr(ATTR_PLACE_CATEGORY)),
                )
        if "name" in self.get_attr(ATTR_OSM_DICT).get("namedetails"):
            self.set_attr(
                ATTR_PLACE_NAME,
                self.get_attr(ATTR_OSM_DICT).get("namedetails").get("name"),
            )
        if not self.is_attr_blank(CONF_LANGUAGE):
            for language in self.get_attr(CONF_LANGUAGE).split(","):
                if "name:" + language in self.get_attr(ATTR_OSM_DICT).get(
                    "namedetails"
                ):
                    self.set_attr(
                        ATTR_PLACE_NAME,
                        self.get_attr(ATTR_OSM_DICT)
                        .get("namedetails")
                        .get("name:" + language),
                    )
                    break
        if not self.in_zone() and self.get_attr(ATTR_PLACE_NAME) != "house":
            self.set_attr(ATTR_NATIVE_VALUE, self.get_attr(ATTR_PLACE_NAME))

        if "house_number" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_STREET_NUMBER,
                (self.get_attr(ATTR_OSM_DICT).get("address").get("house_number")),
            )
        if "road" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_STREET,
                self.get_attr(ATTR_OSM_DICT).get("address").get("road"),
            )

        if "neighbourhood" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_PLACE_NEIGHBOURHOOD,
                self.get_attr(ATTR_OSM_DICT).get("address").get("neighbourhood"),
            )
        elif "hamlet" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_PLACE_NEIGHBOURHOOD,
                self.get_attr(ATTR_OSM_DICT).get("address").get("hamlet"),
            )

        if "city" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_CITY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("city"),
            )
        elif "town" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_CITY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("town"),
            )
        elif "village" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_CITY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("village"),
            )
        elif "township" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_CITY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("township"),
            )
        elif "municipality" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_CITY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("municipality"),
            )
        elif "city_district" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_CITY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("city_district"),
            )
        if not self.is_attr_blank(ATTR_CITY) and self.get_attr(ATTR_CITY).startswith(
            "City of"
        ):
            self.set_attr(ATTR_CITY, self.get_attr(ATTR_CITY)[8:] + " City")

        if "city_district" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_POSTAL_TOWN,
                self.get_attr(ATTR_OSM_DICT).get("address").get("city_district"),
            )
        if "suburb" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_POSTAL_TOWN,
                self.get_attr(ATTR_OSM_DICT).get("address").get("suburb"),
            )
        if "state" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_REGION,
                self.get_attr(ATTR_OSM_DICT).get("address").get("state"),
            )
        if "ISO3166-2-lvl4" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_STATE_ABBR,
                (
                    self.get_attr(ATTR_OSM_DICT)
                    .get("address")
                    .get("ISO3166-2-lvl4")
                    .split("-")[1]
                    .upper()
                ),
            )
        if "county" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_COUNTY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("county"),
            )
        if "country" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_COUNTRY,
                self.get_attr(ATTR_OSM_DICT).get("address").get("country"),
            )
        if "postcode" in self.get_attr(ATTR_OSM_DICT).get("address"):
            self.set_attr(
                ATTR_POSTAL_CODE,
                self.get_attr(ATTR_OSM_DICT).get("address").get("postcode"),
            )
        if "display_name" in self.get_attr(ATTR_OSM_DICT):
            self.set_attr(
                ATTR_FORMATTED_ADDRESS,
                self.get_attr(ATTR_OSM_DICT).get("display_name"),
            )

        if "osm_id" in self.get_attr(ATTR_OSM_DICT):
            self.set_attr(ATTR_OSM_ID, str(self.get_attr(ATTR_OSM_DICT).get("osm_id")))
        if "osm_type" in self.get_attr(ATTR_OSM_DICT):
            self.set_attr(ATTR_OSM_TYPE, self.get_attr(ATTR_OSM_DICT).get("osm_type"))

        if (
            not self.is_attr_blank(ATTR_PLACE_CATEGORY)
            and self.get_attr(ATTR_PLACE_CATEGORY).lower() == "highway"
            and "namedetails" in self.get_attr(ATTR_OSM_DICT)
            and "ref" in self.get_attr(ATTR_OSM_DICT).get("namedetails")
        ):
            street_ref = self.get_attr(ATTR_OSM_DICT).get("namedetails").get("ref")
            exclude_chars = [",", "\\", "/", ";", ":"]
            if 1 in [c in street_ref for c in exclude_chars]:
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Initial Street Ref: "
                    + str(street_ref)
                )
                lowest_nums = []
                for char in exclude_chars:
                    if find_num := street_ref.find(char) != -1:
                        lowest_nums.append(int(find_num))
                lowest_num = int(min(lowest_nums))
                street_ref = street_ref[:lowest_num]
            self.set_attr(
                ATTR_STREET_REF,
                street_ref,
            )
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Street: "
                + str(self.get_attr(ATTR_STREET))
                + " / Street Ref: "
                + str(self.get_attr(ATTR_STREET_REF))
            )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Entity attributes after parsing OSM Dict: "
            + str(self._internal_attr)
        )

    def build_formatted_place(self):
        formatted_place_array = []
        # Don't use place name if the same as another attributes
        use_place_name = True
        sensor_attributes_values = []
        for attr in PLACE_NAME_DUPLICATE_LIST:
            if not self.is_attr_blank(attr):
                sensor_attributes_values.append(self.get_attr(attr))
        if (
            not self.is_attr_blank(ATTR_PLACE_NAME)
            and self.get_attr(ATTR_PLACE_NAME) in sensor_attributes_values
        ):
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Not Using Place Name: "
                + str(self.get_attr(ATTR_PLACE_NAME))
            )
            use_place_name = False
        else:
            use_place_name = False

        display_options = self.get_attr(ATTR_DISPLAY_OPTIONS)
        if not self.in_zone():
            if self.get_attr(ATTR_IS_DRIVING) and "driving" in display_options:
                formatted_place_array.append("Driving")
            if not use_place_name:
                if (
                    not self.is_attr_blank(ATTR_PLACE_TYPE)
                    and self.get_attr(ATTR_PLACE_TYPE).lower() != "unclassified"
                    and self.get_attr(ATTR_PLACE_CATEGORY).lower() != "highway"
                ):
                    formatted_place_array.append(
                        self.get_attr(ATTR_PLACE_TYPE)
                        .title()
                        .replace("Proposed", "")
                        .replace("Construction", "")
                        .strip()
                    )
                elif (
                    not self.is_attr_blank(ATTR_PLACE_CATEGORY)
                    and self.get_attr(ATTR_PLACE_CATEGORY).lower() != "highway"
                ):
                    formatted_place_array.append(
                        self.get_attr(ATTR_PLACE_CATEGORY).title().strip()
                    )
                if not self.is_attr_blank(ATTR_STREET):
                    if (
                        not self.is_attr_blank(ATTR_PLACE_CATEGORY)
                        and self.get_attr(ATTR_PLACE_CATEGORY).lower() == "highway"
                        and not self.is_attr_blank(ATTR_PLACE_TYPE)
                        and self.get_attr(ATTR_PLACE_TYPE).lower()
                        in ["motorway", "trunk"]
                        and not self.is_attr_blank(ATTR_STREET_REF)
                    ):
                        street = self.get_attr(ATTR_STREET_REF).strip()
                    else:
                        street = self.get_attr(ATTR_STREET).strip()
                    if self.is_attr_blank(ATTR_STREET_NUMBER):
                        formatted_place_array.append(street)
                    else:
                        formatted_place_array.append(
                            str(self.get_attr(ATTR_STREET_NUMBER)).strip()
                            + " "
                            + str(street)
                        )
                if (
                    not self.is_attr_blank(ATTR_PLACE_TYPE)
                    and self.get_attr(ATTR_PLACE_TYPE).lower() == "house"
                    and not self.is_attr_blank(ATTR_PLACE_NEIGHBOURHOOD)
                ):
                    formatted_place_array.append(
                        self.get_attr(ATTR_PLACE_NEIGHBOURHOOD).strip()
                    )

            else:
                formatted_place_array.append(self.get_attr(ATTR_PLACE_NAME).strip())
            if not self.is_attr_blank(ATTR_CITY):
                formatted_place_array.append(
                    self.get_attr(ATTR_CITY).replace(" Township", "").strip()
                )
            elif not self.is_attr_blank(ATTR_COUNTY):
                formatted_place_array.append(self.get_attr(ATTR_COUNTY).strip())
            if not self.is_attr_blank(ATTR_STATE_ABBR):
                formatted_place_array.append(self.get_attr(ATTR_STATE_ABBR))
        else:
            formatted_place_array.append(
                self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME).strip()
            )
        formatted_place = ", ".join(item for item in formatted_place_array)
        formatted_place = formatted_place.replace("\n", " ").replace("  ", " ").strip()
        self.set_attr(ATTR_FORMATTED_PLACE, formatted_place)

    def build_state_from_display_options(self):
        # Options:  "formatted_place, driving, zone, zone_name, place_name, place, street_number, street, city, county, state, postal_code, country, formatted_address, do_not_show_not_home"

        display_options = self.get_attr(ATTR_DISPLAY_OPTIONS)
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Building State from Display Options: "
            + str(self.get_attr(ATTR_OPTIONS))
        )

        user_display = []
        if "driving" in display_options and self.get_attr(ATTR_IS_DRIVING):
            user_display.append("Driving")

        if (
            "zone_name" in display_options
            and "do_not_show_not_home" not in display_options
            and not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE_NAME)
        ):
            user_display.append(self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME))
        elif (
            "zone" in display_options
            and "do_not_show_not_home" not in display_options
            and not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE)
        ):
            user_display.append(self.get_attr(ATTR_DEVICETRACKER_ZONE))

        if "place_name" in display_options and not self.is_attr_blank(ATTR_PLACE_NAME):
            user_display.append(self.get_attr(ATTR_PLACE_NAME))
        if "place" in display_options:
            if not self.is_attr_blank(ATTR_PLACE_NAME) and self.get_attr(
                ATTR_PLACE_NAME
            ) != self.get_attr(ATTR_STREET):
                user_display.append(self.get_attr(ATTR_PLACE_NAME))
            if (
                not self.is_attr_blank(ATTR_PLACE_CATEGORY)
                and self.get_attr(ATTR_PLACE_CATEGORY).lower() != "place"
            ):
                user_display.append(self.get_attr(ATTR_PLACE_CATEGORY))
            if (
                not self.is_attr_blank(ATTR_PLACE_TYPE)
                and self.get_attr(ATTR_PLACE_TYPE).lower() != "yes"
            ):
                user_display.append(self.get_attr(ATTR_PLACE_TYPE))
            if not self.is_attr_blank(ATTR_PLACE_NEIGHBOURHOOD):
                user_display.append(self.get_attr(ATTR_PLACE_NEIGHBOURHOOD))
            if not self.is_attr_blank(ATTR_STREET_NUMBER):
                user_display.append(self.get_attr(ATTR_STREET_NUMBER))
            if not self.is_attr_blank(ATTR_STREET):
                user_display.append(self.get_attr(ATTR_STREET))
        else:
            if "street_number" in display_options and not self.is_attr_blank(
                ATTR_STREET_NUMBER
            ):
                user_display.append(self.get_attr(ATTR_STREET_NUMBER))
            if "street" in display_options and not self.is_attr_blank(ATTR_STREET):
                user_display.append(self.get_attr(ATTR_STREET))
        if "city" in display_options and not self.is_attr_blank(ATTR_CITY):
            user_display.append(self.get_attr(ATTR_CITY))
        if "county" in display_options and not self.is_attr_blank(ATTR_COUNTY):
            user_display.append(self.get_attr(ATTR_COUNTY))
        if "state" in display_options and not self.is_attr_blank(ATTR_REGION):
            user_display.append(self.get_attr(ATTR_REGION))
        elif "region" in display_options and not self.is_attr_blank(ATTR_REGION):
            user_display.append(self.get_attr(ATTR_REGION))
        if "postal_code" in display_options and not self.is_attr_blank(
            ATTR_POSTAL_CODE
        ):
            user_display.append(self.get_attr(ATTR_POSTAL_CODE))
        if "country" in display_options and not self.is_attr_blank(ATTR_COUNTRY):
            user_display.append(self.get_attr(ATTR_COUNTRY))
        if "formatted_address" in display_options and not self.is_attr_blank(
            ATTR_FORMATTED_ADDRESS
        ):
            user_display.append(self.get_attr(ATTR_FORMATTED_ADDRESS))

        if "do_not_reorder" in display_options:
            user_display = []
            display_options.remove("do_not_reorder")
            for option in display_options:
                if option == "state":
                    target_option = "region"
                if option == "place_neighborhood":
                    target_option = "place_neighbourhood"
                if option in locals():
                    user_display.append(target_option)

        if user_display:
            self.set_attr(ATTR_NATIVE_VALUE, ", ".join(item for item in user_display))
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") New State from Display Options: "
            + str(self.get_attr(ATTR_NATIVE_VALUE))
        )

    def get_extended_attr(self):
        if not self.is_attr_blank(ATTR_OSM_ID) and not self.is_attr_blank(
            ATTR_OSM_TYPE
        ):
            if self.get_attr(ATTR_OSM_TYPE).lower() == "node":
                osm_type_abbr = "N"
            elif self.get_attr(ATTR_OSM_TYPE).lower() == "way":
                osm_type_abbr = "W"
            elif self.get_attr(ATTR_OSM_TYPE).lower() == "relation":
                osm_type_abbr = "R"

            osm_details_url = (
                "https://nominatim.openstreetmap.org/details.php?osmtype="
                + str(osm_type_abbr)
                + "&osmid="
                + str(self.get_attr(ATTR_OSM_ID))
                + "&linkedplaces=1&hierarchy=1&group_hierarchy=1&limit=1&format=json"
                + (
                    "&email=" + str(self.get_attr(CONF_API_KEY))
                    if self.is_attr_blank(CONF_API_KEY)
                    else ""
                )
                + (
                    "&accept-language=" + str(self.get_attr(CONF_LANGUAGE))
                    if not self.is_attr_blank(CONF_LANGUAGE)
                    else ""
                )
            )
            self.set_attr(
                ATTR_OSM_DETAILS_DICT,
                self.get_dict_from_url(osm_details_url, "OpenStreetMaps Details"),
            )

            if not self.is_attr_blank(ATTR_OSM_DETAILS_DICT):
                # _LOGGER.debug("(" + self.get_attr(CONF_NAME) + ") OSM Details Dict: " + str(osm_details_dict))

                if (
                    not self.is_attr_blank(ATTR_OSM_DETAILS_DICT)
                    and "extratags" in self.get_attr(ATTR_OSM_DETAILS_DICT)
                    and "wikidata"
                    in self.get_attr(ATTR_OSM_DETAILS_DICT).get("extratags")
                ):
                    self.set_attr(
                        ATTR_WIKIDATA_ID,
                        self.get_attr(ATTR_OSM_DETAILS_DICT)
                        .get("extratags")
                        .get("wikidata"),
                    )

                self.set_attr(ATTR_WIKIDATA_DICT, {})
                if not self.is_attr_blank(ATTR_WIKIDATA_ID):
                    wikidata_url = (
                        "https://www.wikidata.org/wiki/Special:EntityData/"
                        + str(self.get_attr(ATTR_WIKIDATA_ID))
                        + ".json"
                    )
                    self.set_attr(
                        ATTR_WIKIDATA_DICT,
                        self.get_dict_from_url(wikidata_url, "Wikidata"),
                    )

    def fire_event_data(self, prev_last_place_name):
        _LOGGER.debug("(" + self.get_attr(CONF_NAME) + ") Building Event Data")
        event_data = {}
        if not self.is_attr_blank(CONF_NAME):
            event_data.update({"entity": self.get_attr(CONF_NAME)})
        if not self.is_attr_blank(ATTR_PREVIOUS_STATE):
            event_data.update({"from_state": self.get_attr(ATTR_PREVIOUS_STATE)})
        if not self.is_attr_blank(ATTR_NATIVE_VALUE):
            event_data.update({"to_state": self.get_attr(ATTR_NATIVE_VALUE)})

        for attr in EVENT_ATTRIBUTE_LIST:
            if not self.is_attr_blank(attr):
                event_data.update({attr: self.get_attr(attr)})

        if (
            not self.is_attr_blank(ATTR_LAST_PLACE_NAME)
            and self.get_attr(ATTR_LAST_PLACE_NAME) != prev_last_place_name
        ):
            event_data.update(
                {ATTR_LAST_PLACE_NAME: self.get_attr(ATTR_LAST_PLACE_NAME)}
            )

        if self.get_attr(CONF_EXTENDED_ATTR):
            for attr in EXTENDED_ATTRIBUTE_LIST:
                if not self.is_attr_blank(attr):
                    event_data.update({attr: self.get_attr(attr)})

        self._hass.bus.fire(DOMAIN + "_state_update", event_data)
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Event Details [event_type: "
            + DOMAIN
            + "_state_update]: "
            + str(event_data)
        )
        _LOGGER.info(
            "("
            + self.get_attr(CONF_NAME)
            + ") Event Fired [event_type: "
            + DOMAIN
            + "_state_update]"
        )

    def write_sensor_to_json(self):
        sensor_attributes = copy.deepcopy(self._internal_attr)
        for k, v in list(sensor_attributes.items()):
            if isinstance(v, (datetime)):
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Removing Sensor Attribute: "
                    + str(k)
                )
                sensor_attributes.pop(k)
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") Sensor Attributes to Save: "
        #    + str(sensor_attributes)
        # )
        try:
            with open(
                os.path.join(PLACES_JSON_FOLDER, self.get_attr(ATTR_JSON_FILENAME)),
                "w",
            ) as jsonfile:
                json.dump(sensor_attributes, jsonfile)
        except OSError as e:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") OSError writing sensor to JSON ("
                + str(self.get_attr(ATTR_JSON_FILENAME))
                + "): "
                + str(e)
            )
        except Exception as e:
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Unknown Exception writing sensor to JSON ("
                + str(self.get_attr(ATTR_JSON_FILENAME))
                + "): "
                + str(e)
            )

    def update_coordinates_and_distance(self):
        last_distance_m = self.get_attr(ATTR_DISTANCE_FROM_HOME_M)
        proceed_with_update = True
        if not self.is_attr_blank(ATTR_LATITUDE) and not self.is_attr_blank(
            ATTR_LONGITUDE
        ):
            self.set_attr(
                ATTR_LOCATION_CURRENT,
                (
                    str(self.get_attr(ATTR_LATITUDE))
                    + ","
                    + str(self.get_attr(ATTR_LONGITUDE))
                ),
            )
        if not self.is_attr_blank(ATTR_LATITUDE_OLD) and not self.is_attr_blank(
            ATTR_LONGITUDE_OLD
        ):
            self.set_attr(
                ATTR_LOCATION_PREVIOUS,
                (
                    str(self.get_attr(ATTR_LATITUDE_OLD))
                    + ","
                    + str(self.get_attr(ATTR_LONGITUDE_OLD))
                ),
            )
        if not self.is_attr_blank(ATTR_HOME_LATITUDE) and not self.is_attr_blank(
            ATTR_HOME_LONGITUDE
        ):
            self.set_attr(
                ATTR_HOME_LOCATION,
                (
                    str(self.get_attr(ATTR_HOME_LATITUDE))
                    + ","
                    + str(self.get_attr(ATTR_HOME_LONGITUDE))
                ),
            )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Previous last_place_name: "
            + str(self.get_attr(ATTR_LAST_PLACE_NAME))
        )

        if not self.in_zone():
            # Not in a Zone
            if not self.is_attr_blank(ATTR_PLACE_NAME):
                # If place name is set
                self.set_attr(ATTR_LAST_PLACE_NAME, self.get_attr(ATTR_PLACE_NAME))
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Previous place is Place Name, last_place_name is set: "
                    + str(self.get_attr(ATTR_LAST_PLACE_NAME))
                )
            else:
                # If blank, keep previous last place name
                _LOGGER.debug(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Previous Place Name is None, keeping prior"
                )
        else:
            # In a Zone
            self.set_attr(
                ATTR_LAST_PLACE_NAME, self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME)
            )
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Previous Place is Zone: "
                + str(self.get_attr(ATTR_LAST_PLACE_NAME))
            )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Last Place Name (Initial): "
            + str(self.get_attr(ATTR_LAST_PLACE_NAME))
        )

        if (
            not self.is_attr_blank(ATTR_LATITUDE)
            and not self.is_attr_blank(ATTR_LONGITUDE)
            and not self.is_attr_blank(ATTR_HOME_LATITUDE)
            and not self.is_attr_blank(ATTR_HOME_LONGITUDE)
        ):
            self.set_attr(
                ATTR_DISTANCE_FROM_HOME_M,
                distance(
                    float(self.get_attr(ATTR_LATITUDE)),
                    float(self.get_attr(ATTR_LONGITUDE)),
                    float(self.get_attr(ATTR_HOME_LATITUDE)),
                    float(self.get_attr(ATTR_HOME_LONGITUDE)),
                ),
            )
            if not self.is_attr_blank(ATTR_DISTANCE_FROM_HOME_M):
                self.set_attr(
                    ATTR_DISTANCE_FROM_HOME_KM,
                    round(self.get_attr(ATTR_DISTANCE_FROM_HOME_M) / 1000, 3),
                )
                self.set_attr(
                    ATTR_DISTANCE_FROM_HOME_MI,
                    round(self.get_attr(ATTR_DISTANCE_FROM_HOME_M) / 1609, 3),
                )
            if not self.is_attr_blank(ATTR_LATITUDE_OLD) and not self.is_attr_blank(
                ATTR_LONGITUDE_OLD
            ):
                deviation = self.haversine(
                    float(self.get_attr(ATTR_LATITUDE_OLD)),
                    float(self.get_attr(ATTR_LONGITUDE_OLD)),
                    float(self.get_attr(ATTR_LATITUDE)),
                    float(self.get_attr(ATTR_LONGITUDE)),
                )
                if deviation <= 0.2:  # in kilometers
                    self.set_attr(ATTR_DIRECTION_OF_TRAVEL, "stationary")
                elif last_distance_m > self.get_attr(ATTR_DISTANCE_FROM_HOME_M):
                    self.set_attr(ATTR_DIRECTION_OF_TRAVEL, "towards home")
                elif last_distance_m < self.get_attr(ATTR_DISTANCE_FROM_HOME_M):
                    self.set_attr(ATTR_DIRECTION_OF_TRAVEL, "away from home")
                else:
                    self.set_attr(ATTR_DIRECTION_OF_TRAVEL, "stationary")
            else:
                self.set_attr(ATTR_DIRECTION_OF_TRAVEL, "stationary")

            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Previous Location: "
                + str(self.get_attr(ATTR_LOCATION_PREVIOUS))
            )
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Current Location: "
                + str(self.get_attr(ATTR_LOCATION_CURRENT))
            )
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Home Location: "
                + str(self.get_attr(ATTR_HOME_LOCATION))
            )
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Distance from home ["
                + (self.get_attr(CONF_HOME_ZONE)).split(".")[1]
                + "]: "
                + str(self.get_attr(ATTR_DISTANCE_FROM_HOME_KM))
                + " km"
            )
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Travel Direction: "
                + str(self.get_attr(ATTR_DIRECTION_OF_TRAVEL))
            )

            self.set_attr(ATTR_DISTANCE_TRAVELED_M, 0)
            if not self.is_attr_blank(ATTR_LATITUDE_OLD) and not self.is_attr_blank(
                ATTR_LONGITUDE_OLD
            ):
                self.set_attr(
                    ATTR_DISTANCE_TRAVELED_M,
                    distance(
                        float(self.get_attr(ATTR_LATITUDE)),
                        float(self.get_attr(ATTR_LONGITUDE)),
                        float(self.get_attr(ATTR_LATITUDE_OLD)),
                        float(self.get_attr(ATTR_LONGITUDE_OLD)),
                    ),
                )
                if not self.is_attr_blank(ATTR_DISTANCE_TRAVELED_M):
                    self.set_attr(
                        ATTR_DISTANCE_TRAVELED_MI,
                        round(self.get_attr(ATTR_DISTANCE_TRAVELED_M) / 1609, 3),
                    )

            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Meters traveled since last update: "
                + str(round(self.get_attr(ATTR_DISTANCE_TRAVELED_M), 1))
            )
        else:
            proceed_with_update = False
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Problem with updated lat/long, not performing update: "
                + "old_latitude="
                + str(self.get_attr(ATTR_LATITUDE_OLD))
                + ", old_longitude="
                + str(self.get_attr(ATTR_LONGITUDE_OLD))
                + ", new_latitude="
                + str(self.get_attr(ATTR_LATITUDE))
                + ", new_longitude="
                + str(self.get_attr(ATTR_LONGITUDE))
                + ", home_latitude="
                + str(self.get_attr(ATTR_HOME_LATITUDE))
                + ", home_longitude="
                + str(self.get_attr(ATTR_HOME_LONGITUDE))
            )
        return proceed_with_update

    def do_update(self, reason):
        """Get the latest data and updates the states."""

        now = datetime.now()
        previous_attr = copy.deepcopy(self._internal_attr)

        _LOGGER.info("(" + self.get_attr(CONF_NAME) + ") Starting Update...")
        self.check_for_updated_entity_name()
        self.cleanup_attributes()
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") Previous entity attributes: "
        #    + str(self._internal_attr)
        # )
        if not self.is_attr_blank(ATTR_NATIVE_VALUE) and self.get_attr(CONF_SHOW_TIME):
            self.set_attr(
                ATTR_PREVIOUS_STATE, str(self.get_attr(ATTR_NATIVE_VALUE)[:-14])
            )
        else:
            self.set_attr(ATTR_PREVIOUS_STATE, self.get_attr(ATTR_NATIVE_VALUE))
        if self.is_float(self.get_attr(ATTR_LATITUDE)):
            self.set_attr(ATTR_LATITUDE_OLD, str(self.get_attr(ATTR_LATITUDE)))
        if self.is_float(self.get_attr(ATTR_LONGITUDE)):
            self.set_attr(ATTR_LONGITUDE_OLD, str(self.get_attr(ATTR_LONGITUDE)))
        prev_last_place_name = self.get_attr(ATTR_LAST_PLACE_NAME)

        _LOGGER.info(
            "(" + self.get_attr(CONF_NAME) + ") Calling update due to: " + str(reason)
        )
        _LOGGER.info(
            "("
            + self.get_attr(CONF_NAME)
            + ") Check if update required for: "
            + str(self.get_attr(CONF_DEVICETRACKER_ID))
        )
        _LOGGER.debug(
            "("
            + self.get_attr(CONF_NAME)
            + ") Previous State: "
            + str(self.get_attr(ATTR_PREVIOUS_STATE))
        )

        if self.is_float(
            self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes.get(
                CONF_LATITUDE
            )
        ):
            self.set_attr(
                ATTR_LATITUDE,
                str(
                    self._hass.states.get(
                        self.get_attr(CONF_DEVICETRACKER_ID)
                    ).attributes.get(CONF_LATITUDE)
                ),
            )
        if self.is_float(
            self._hass.states.get(self.get_attr(CONF_DEVICETRACKER_ID)).attributes.get(
                CONF_LONGITUDE
            )
        ):
            self.set_attr(
                ATTR_LONGITUDE,
                str(
                    self._hass.states.get(
                        self.get_attr(CONF_DEVICETRACKER_ID)
                    ).attributes.get(CONF_LONGITUDE)
                ),
            )

        self.get_gps_accuracy()
        self.get_zone_details()
        proceed_with_update = self.update_coordinates_and_distance()

        if proceed_with_update:
            proceed_with_update = self.determine_if_update_needed()

        if proceed_with_update and not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE):
            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") Meets criteria, proceeding with OpenStreetMap query"
            )

            _LOGGER.info(
                "("
                + self.get_attr(CONF_NAME)
                + ") DeviceTracker Zone: "
                + str(self.get_attr(ATTR_DEVICETRACKER_ZONE))
                + " / Skipped Updates: "
                + str(self.get_attr(ATTR_UPDATES_SKIPPED))
            )

            self._reset_attributes()
            self.get_map_link()

            osm_url = (
                "https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat="
                + str(self.get_attr(ATTR_LATITUDE))
                + "&lon="
                + str(self.get_attr(ATTR_LONGITUDE))
                + (
                    "&accept-language=" + str(self.get_attr(CONF_LANGUAGE))
                    if not self.is_attr_blank(CONF_LANGUAGE)
                    else ""
                )
                + "&addressdetails=1&namedetails=1&zoom=18&limit=1"
                + (
                    "&email=" + str(self.get_attr(CONF_API_KEY))
                    if not self.is_attr_blank(CONF_API_KEY)
                    else ""
                )
            )

            self.set_attr(
                ATTR_OSM_DICT, self.get_dict_from_url(osm_url, "OpenStreetMaps")
            )
            if not self.is_attr_blank(ATTR_OSM_DICT):

                self.parse_osm_dict()
                if self.get_attr(ATTR_INITIAL_UPDATE):
                    self.set_attr(ATTR_LAST_PLACE_NAME, prev_last_place_name)
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") Runnining initial update after load, using prior last_place_name"
                    )
                elif self.get_attr(ATTR_LAST_PLACE_NAME) == self.get_attr(
                    ATTR_PLACE_NAME
                ) or self.get_attr(ATTR_LAST_PLACE_NAME) == self.get_attr(
                    ATTR_DEVICETRACKER_ZONE_NAME
                ):
                    # If current place name/zone are the same as previous, keep older last place name
                    self.set_attr(ATTR_LAST_PLACE_NAME, prev_last_place_name)
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") Initial last_place_name is same as new: place_name="
                        + str(self.get_attr(ATTR_PLACE_NAME))
                        + " or devicetracker_zone_name="
                        + str(self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME))
                        + ", keeping previous last_place_name"
                    )
                else:
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") Keeping initial last_place_name"
                    )
                _LOGGER.info(
                    "("
                    + self.get_attr(CONF_NAME)
                    + ") Last Place Name: "
                    + str(self.get_attr(ATTR_LAST_PLACE_NAME))
                )

                display_options = []
                if not self.is_attr_blank(CONF_OPTIONS):
                    options_array = self.get_attr(ATTR_OPTIONS).split(",")
                    for option in options_array:
                        display_options.append(option.strip())
                self.set_attr(ATTR_DISPLAY_OPTIONS, display_options)

                self.get_driving_status()

                if "formatted_place" in display_options:
                    self.build_formatted_place()
                    self.set_attr(
                        ATTR_NATIVE_VALUE, self.get_attr(ATTR_FORMATTED_PLACE)
                    )
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") New State using formatted_place: "
                        + str(self.get_attr(ATTR_NATIVE_VALUE))
                    )
                elif not self.in_zone():
                    self.build_state_from_display_options()
                elif (
                    "zone" in display_options
                    and not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE)
                ) or self.is_attr_blank(ATTR_DEVICETRACKER_ZONE_NAME):
                    self.set_attr(
                        ATTR_NATIVE_VALUE, self.get_attr(ATTR_DEVICETRACKER_ZONE)
                    )
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") New State from DeviceTracker Zone: "
                        + str(self.get_attr(ATTR_NATIVE_VALUE))
                    )
                elif not self.is_attr_blank(ATTR_DEVICETRACKER_ZONE_NAME):
                    self.set_attr(
                        ATTR_NATIVE_VALUE, self.get_attr(ATTR_DEVICETRACKER_ZONE_NAME)
                    )
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") New State from DeviceTracker Zone Name: "
                        + str(self.get_attr(ATTR_NATIVE_VALUE))
                    )
                current_time = "%02d:%02d" % (now.hour, now.minute)
                self.set_attr(ATTR_LAST_CHANGED, str(now))

                # Final check to see if the New State is different from the Previous State and should update or not.
                # If not, attributes are reset to what they were before the update started.

                if (
                    (
                        not self.is_attr_blank(ATTR_PREVIOUS_STATE)
                        and not self.is_attr_blank(ATTR_NATIVE_VALUE)
                        and self.get_attr(ATTR_PREVIOUS_STATE).lower().strip()
                        != self.get_attr(ATTR_NATIVE_VALUE).lower().strip()
                        and self.get_attr(ATTR_PREVIOUS_STATE)
                        .replace(" ", "")
                        .lower()
                        .strip()
                        != self.get_attr(ATTR_NATIVE_VALUE).lower().strip()
                        and self.get_attr(ATTR_PREVIOUS_STATE).lower().strip()
                        != self.get_attr(ATTR_DEVICETRACKER_ZONE).lower().strip()
                    )
                    or self.is_attr_blank(ATTR_PREVIOUS_STATE)
                    or self.is_attr_blank(ATTR_NATIVE_VALUE)
                    or self.get_attr(ATTR_INITIAL_UPDATE)
                ):

                    if self.get_attr(CONF_EXTENDED_ATTR):
                        self.get_extended_attr()
                    self.cleanup_attributes()
                    if not self.is_attr_blank(ATTR_NATIVE_VALUE):
                        if self.get_attr(CONF_SHOW_TIME):
                            self.set_attr(
                                ATTR_NATIVE_VALUE,
                                self.get_attr(ATTR_NATIVE_VALUE)[: 255 - 14]
                                + " (since "
                                + current_time
                                + ")",
                            )
                        else:
                            self.set_attr(
                                ATTR_NATIVE_VALUE,
                                self.get_attr(ATTR_NATIVE_VALUE)[:255],
                            )
                        _LOGGER.info(
                            "("
                            + self.get_attr(CONF_NAME)
                            + ") New State: "
                            + str(self.get_attr(ATTR_NATIVE_VALUE))
                        )
                    else:
                        self.clear_attr(ATTR_NATIVE_VALUE)
                        _LOGGER.warning(
                            "(" + self.get_attr(CONF_NAME) + ") New State is None"
                        )
                    if not self.is_attr_blank(ATTR_NATIVE_VALUE):
                        self._attr_native_value = self.get_attr(ATTR_NATIVE_VALUE)
                    else:
                        self._attr_native_value = None
                    self.fire_event_data(prev_last_place_name)
                    self.set_attr(ATTR_INITIAL_UPDATE, False)
                    self.write_sensor_to_json()
                else:
                    self._internal_attr = previous_attr
                    _LOGGER.info(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") No entity update needed, Previous State = New State"
                    )
                    _LOGGER.debug(
                        "("
                        + self.get_attr(CONF_NAME)
                        + ") Reverting attributes back to before the update started"
                    )
        else:
            self._internal_attr = previous_attr
            _LOGGER.debug(
                "("
                + self.get_attr(CONF_NAME)
                + ") Reverting attributes back to before the update started"
            )
        self.set_attr(ATTR_LAST_UPDATED, str(now))
        # _LOGGER.debug(
        #    "("
        #    + self.get_attr(CONF_NAME)
        #    + ") Final entity attributes: "
        #    + str(self._internal_attr)
        # )
        _LOGGER.info("(" + self.get_attr(CONF_NAME) + ") End of Update")

    def _reset_attributes(self):
        """Resets attributes."""
        for attr in RESET_ATTRIBUTE_LIST:
            self.clear_attr(attr)
        self.set_attr(ATTR_UPDATES_SKIPPED, 0)
        self.cleanup_attributes()
