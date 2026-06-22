"""Representation of a OCPP 1.6 charging station."""

from datetime import datetime, timedelta, UTC
import logging

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTime
import voluptuous as vol
from websockets.asyncio.server import ServerConnection

from ocpp.routing import on
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    Measurand,
    MessageTrigger,
    Phase,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnlockStatus,
)

from .chargepoint import (
    OcppVersion,
    MeasurandValue,
    SetVariableResult,
)
from .chargepoint import ChargePoint as cp

from .enums import (
    ConfigurationKey as ckey,
    HAChargerDetails as cdet,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    HAEVBoxStatuses as evbox,
    OcppMisc as om,
    Profiles as prof,
)

from .const import (
    CentralSystemSettings,
    ChargerSystemSettings,
    DEFAULT_MEASURAND,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    MEASURANDS,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)


def _to_message_trigger(name: str) -> MessageTrigger | None:
    if isinstance(name, MessageTrigger):
        return name
    key = str(name).strip().replace(" ", "").replace("_", "").lower()
    mapping = {
        "bootnotification": MessageTrigger.boot_notification,
        "heartbeat": MessageTrigger.heartbeat,
        "metervalues": MessageTrigger.meter_values,
        "statusnotification": MessageTrigger.status_notification,
        "diagnosticsstatusnotification": MessageTrigger.diagnostics_status_notification,
        "firmwarestatusnotification": MessageTrigger.firmware_status_notification,
    }
    return mapping.get(key)


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        id: str,
        connection: ServerConnection,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        charger: ChargerSystemSettings,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(
            id,
            connection,
            OcppVersion.V16,
            hass,
            entry,
            central,
            charger,
        )
        self._active_tx: dict[int, int] = {}  # connector_id -> transaction_id

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        resp = None

        try:
            req = call.GetConfiguration(key=["NumberOfConnectors"])
            resp = await self.call(req)
        except Exception:
            resp = None

        cfg = None
        if resp is not None:
            cfg = getattr(resp, "configuration_key", None)

            if (
                cfg is None
                and isinstance(resp, list | tuple)
                and len(resp) >= 3
                and isinstance(resp[2], dict)
            ):
                cfg = resp[2].get("configurationKey") or resp[2].get(
                    "configuration_key"
                )

        if cfg:
            for kv in cfg:
                k = getattr(kv, "key", None)
                v = getattr(kv, "value", None)
                if k is None and isinstance(kv, dict):
                    k = kv.get("key")
                    v = kv.get("value")
                if k == "NumberOfConnectors" and v not in (None, ""):
                    try:
                        n = int(str(v).strip())
                        if n > 0:
                            return n
                    except (ValueError, TypeError):
                        pass

        return 1

    async def get_heartbeat_interval(self):
        """Retrieve heartbeat interval from the charger and store it."""
        await self.get_configuration(ckey.heartbeat_interval.value)

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""

        def _filter_measurands(raw_csv: str) -> str:
            """Keep only compliant measurands found as tokens in the charger's string."""
            # Protect against empty lists and the "Unknown" sentinel (checked by test_measurands_manual_set_rejected_returns_empty)
            if not raw_csv or raw_csv.strip().lower() == "unknown":
                return ""

            matched = []
            for token in raw_csv.split(","):
                token = token.strip()
                if not token:
                    continue

                for m in MEASURANDS:
                    # Token-aware match: Exact match OR prefix match with a dot (e.g. "Voltage.L1")
                    if token == m or token.startswith(f"{m}."):
                        if m not in matched:
                            matched.append(m)
                        break  # Match found for this token, move to the next one

            if not matched:
                _LOGGER.debug(
                    "Charger '%s' returned no valid measurands; falling back to %s.",
                    self.id,
                    DEFAULT_MEASURAND,
                )
                return DEFAULT_MEASURAND

            return ",".join(matched)

        all_measurands = self.settings.monitored_variables or ""
        autodetect_measurands = bool(self.settings.monitored_variables_autoconfig)
        key = ckey.meter_values_sampled_data.value

        desired_csv = all_measurands.strip().strip(",")
        cfg_ok = {ConfigurationStatus.accepted, ConfigurationStatus.reboot_required}

        effective_csv: str = ""

        if autodetect_measurands:
            if desired_csv:
                _LOGGER.debug(
                    "'%s' attempting CSV set for measurands: %s", self.id, desired_csv
                )
                try:
                    resp = await self.call(
                        call.ChangeConfiguration(key=key, value=desired_csv)
                    )
                    if getattr(resp, "status", None) in cfg_ok:
                        _LOGGER.debug(
                            "'%s' measurands CSV accepted with status=%s",
                            self.id,
                            resp.status,
                        )
                        effective_csv = desired_csv
                    else:
                        _LOGGER.debug(
                            "'%s' measurands CSV rejected with status=%s; falling back to GetConfiguration",
                            self.id,
                            getattr(resp, "status", None),
                        )
                except Exception as ex:
                    _LOGGER.debug(
                        "get_supported_measurands CSV set raised for '%s': %s",
                        self.id,
                        ex,
                    )

            # Read from charger and filter it using lenient logic
            chgr_csv = await self.get_configuration(key)
            chgr_csv = _filter_measurands(chgr_csv)

            if not effective_csv:
                _LOGGER.debug(
                    "'%s' measurands not configurable by integration", self.id
                )
                _LOGGER.debug("'%s' allowed measurands: '%s'", self.id, chgr_csv)
                return chgr_csv

            _LOGGER.debug(
                "Returning accepted measurands for '%s': '%s'", self.id, effective_csv
            )
            await self.configure(key, effective_csv)
            return effective_csv

        # Non-autodetect path:
        if desired_csv:
            try:
                resp = await self.call(
                    call.ChangeConfiguration(key=key, value=desired_csv)
                )
                _LOGGER.debug(
                    "'%s' measurands set manually to %s", self.id, desired_csv
                )
                if getattr(resp, "status", None) in cfg_ok:
                    effective_csv = desired_csv
                else:
                    _LOGGER.debug(
                        "'%s' manual measurands set not accepted (status=%s); using charger's value",
                        self.id,
                        getattr(resp, "status", None),
                    )
                    effective_csv = await self.get_configuration(key)
            except Exception as ex:
                _LOGGER.debug(
                    "Manual measurands set failed for '%s': %s; using charger's value",
                    self.id,
                    ex,
                )
                effective_csv = await self.get_configuration(key)
        else:
            effective_csv = await self.get_configuration(key)

        # Filter whatever resulted from the manual path
        effective_csv = _filter_measurands(effective_csv)

        if effective_csv:
            _LOGGER.debug("'%s' allowed measurands: '%s'", self.id, effective_csv)
            await self.configure(key, effective_csv)
        else:
            _LOGGER.debug("'%s' measurands not configurable by integration", self.id)

        return effective_csv

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        await self.configure(
            ckey.meter_value_sample_interval.value,
            str(self.settings.meter_interval),
        )
        await self.configure(
            ckey.clock_aligned_data_interval.value,
            str(self.settings.idle_interval),
        )

    async def get_supported_features(self) -> prof:
        """Get features supported by the charger."""
        features = prof.NONE
        req = call.GetConfiguration(key=[ckey.supported_feature_profiles.value])
        resp = await self.call(req)
        try:
            feature_list = (resp.configuration_key[0][om.value.value]).split(",")
        except (IndexError, KeyError, TypeError):
            feature_list = [""]
        if feature_list[0] == "":
            _LOGGER.warning("No feature profiles detected, defaulting to Core")
            await self.notify_ha("No feature profiles detected, defaulting to Core")
            feature_list = [om.feature_profile_core.value]

        if self.settings.force_smart_charging:
            _LOGGER.warning("Force Smart Charging feature profile")
            features |= prof.SMART

        for item in feature_list:
            item = item.strip().replace(" ", "")
            if item == om.feature_profile_core.value:
                features |= prof.CORE
            elif item == om.feature_profile_firmware.value:
                features |= prof.FW
            elif item == om.feature_profile_smart.value:
                features |= prof.SMART
            elif item == om.feature_profile_reservation.value:
                features |= prof.RES
            elif item == om.feature_profile_remote.value:
                features |= prof.REM
            elif item == om.feature_profile_auth.value:
                features |= prof.AUTH
            else:
                _LOGGER.warning("Unknown feature profile detected ignoring: %s", item)
                await self.notify_ha(
                    f"Warning: Unknown feature profile detected ignoring {item}"
                )
        return features

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        req = call.TriggerMessage(requested_message=MessageTrigger.boot_notification)
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            self.triggered_boot_notification = True
            return True
        else:
            self.triggered_boot_notification = False
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        try:
            n = int(self._metrics[0][cdet.connectors.value].value or 1)
        except Exception:
            n = 1

        # Single connector: only probe 1. Multi: probe 0 then 1..n.
        attempts = [1] if n <= 1 else [0] + list(range(1, n + 1))

        for cid in attempts:
            _LOGGER.debug("trigger status notification for connector=%s", cid)
            try:
                req = call.TriggerMessage(
                    requested_message=MessageTrigger.status_notification,
                    connector_id=int(cid),
                )
                resp = await self.call(req)
                status = getattr(resp, "status", None)
            except Exception as ex:
                _LOGGER.debug("TriggerMessage failed for connector=%s: %s", cid, ex)
                status = None

            if status != TriggerMessageStatus.accepted:
                if cid > 0:
                    _LOGGER.warning("Failed with response: %s", status)
                    # Reduce to the last known-good connector index.
                    self._metrics[0][cdet.connectors.value].value = max(1, cid - 1)
                    return False
                # If connector 0 is rejected, continue probing numbered connectors.

        return True

    async def trigger_custom_message(
        self,
        requested_message: str | MessageTrigger = "StatusNotification",
    ):
        """Trigger Custom Message."""
        trig = _to_message_trigger(requested_message)
        if trig is None:
            _LOGGER.warning("Unsupported TriggerMessage: %s", requested_message)
            return False

        req = call.TriggerMessage(requested_message=trig)
        resp = await self.call(req)
        if resp.status != TriggerMessageStatus.accepted:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False
        return True

    async def clear_profile(
        self,
        conn_id: int | None = None,
        purpose: ChargingProfilePurposeType | None = None,
    ) -> bool:
        """Clear charging profiles (per connector and/or purpose)."""
        try:
            req = call.ClearChargingProfile(
                connector_id=(int(conn_id) if conn_id is not None else None),
                charging_profile_purpose=(purpose.value if purpose else None),
            )
            resp = await self.call(req)
            return resp.status in (
                ClearChargingProfileStatus.accepted,
                ClearChargingProfileStatus.unknown,
            )
        except Exception as ex:
            _LOGGER.debug("ClearChargingProfile raised %s (ignored)", ex)
            return False

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
    ) -> bool:
        """Set charge rate."""
        if profile is not None:
            try:
                req = call.SetChargingProfile(
                    connector_id=int(conn_id), cs_charging_profiles=profile
                )
                resp = await self.call(req)
                if resp.status == ChargingProfileStatus.accepted:
                    return True
                _LOGGER.warning("Custom SetChargingProfile rejected: %s", resp.status)
            except Exception as ex:
                _LOGGER.warning("Custom SetChargingProfile failed: %s", ex)
                await self.notify_ha(
                    "Warning: Set charging profile failed with response Exception"
                )
            return False

        if not (int(self.supported_features or 0) & prof.SMART):
            _LOGGER.info("Smart charging is not supported by this charger")
            return False

        # Determine allowed unit (default to Amps if not reported)
        units_resp = await self.get_configuration(
            ckey.charging_schedule_allowed_charging_rate_unit.value
        )
        if not units_resp:
            _LOGGER.debug("Charging rate unit not reported; assuming Amps")
            units_resp = om.current.value

        use_amps = om.current.value in units_resp
        limit_value = float(limit_amps if use_amps else limit_watts)
        units_value = (
            ChargingRateUnitType.amps.value
            if use_amps
            else ChargingRateUnitType.watts.value
        )

        try:
            stack_level_resp = await self.get_configuration(
                ckey.charge_profile_max_stack_level.value
            )
            stack_level = int(stack_level_resp)
        except Exception:
            stack_level = 1

        # Helper to build a simple relative schedule with one period
        def _mk_schedule(_units: str, _limit: float) -> dict:
            return {
                om.charging_rate_unit.value: _units,
                om.charging_schedule_period.value: [
                    {om.start_period.value: 0, om.limit.value: _limit}
                ],
            }

        # Helper to generate a unique, stable chargingProfileId per purpose+connector
        def _profile_id(purpose: str, cid: int) -> int:
            base = {
                ChargingProfilePurposeType.charge_point_max_profile.value: 1000,
                ChargingProfilePurposeType.tx_default_profile.value: 2000,
                ChargingProfilePurposeType.tx_profile.value: 3000,
            }.get(purpose, 9000)
            try:
                n = int(cid or 0)
            except Exception:
                n = 0
            return base + max(0, n)

        # Try ChargePointMaxProfile (connectorId = 0)
        try:
            req = call.SetChargingProfile(
                connector_id=0,
                cs_charging_profiles={
                    om.charging_profile_id.value: _profile_id(
                        ChargingProfilePurposeType.charge_point_max_profile.value, 0
                    ),
                    om.stack_level.value: stack_level,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.charge_point_max_profile.value,
                    om.charging_schedule.value: _mk_schedule(units_value, limit_value),
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            _LOGGER.debug(
                "ChargePointMaxProfile not accepted (%s); will continue.",
                resp.status,
            )
        except Exception as ex:
            _LOGGER.debug("ChargePointMaxProfile call raised: %s", ex)

        # Target connector (default 1 if unspecified/0)
        target_cid = int(conn_id) if conn_id and int(conn_id) > 0 else 1

        # Read active transaction on this connector
        try:
            active_tx_id = int(self._active_tx.get(target_cid, 0) or 0)
        except Exception:
            active_tx_id = 0

        txp_ok = False
        txd_ok = False

        # If an active transaction exists on this connector, try TxProfile first (affects ongoing charging)
        if active_tx_id > 0:
            try:
                txp_stack = max(1, stack_level)  # keep same or higher than defaults
                req = call.SetChargingProfile(
                    connector_id=target_cid,
                    cs_charging_profiles={
                        om.charging_profile_id.value: _profile_id(
                            ChargingProfilePurposeType.tx_profile.value, target_cid
                        ),
                        om.stack_level.value: txp_stack,
                        om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                        om.charging_profile_purpose.value: ChargingProfilePurposeType.tx_profile.value,
                        om.charging_schedule.value: _mk_schedule(
                            units_value, limit_value
                        ),
                        # Bind to the ongoing transaction
                        om.transaction_id.value: active_tx_id,
                    },
                )
                resp = await self.call(req)
                if resp.status == ChargingProfileStatus.accepted:
                    txp_ok = True
                else:
                    _LOGGER.debug("TxProfile not accepted (%s).", resp.status)
            except Exception as ex:
                _LOGGER.debug("TxProfile call raised: %s.", ex)

        # Always attempt TxDefaultProfile as well (for future sessions)
        try:
            tx_stack = max(
                1, stack_level - 1
            )  # slightly lower to avoid overriding TxProfile
            req = call.SetChargingProfile(
                connector_id=target_cid,
                cs_charging_profiles={
                    om.charging_profile_id.value: _profile_id(
                        ChargingProfilePurposeType.tx_default_profile.value, target_cid
                    ),
                    om.stack_level.value: tx_stack,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.tx_default_profile.value,
                    om.charging_schedule.value: _mk_schedule(units_value, limit_value),
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                txd_ok = True
            else:
                _LOGGER.debug("Set TxDefaultProfile rejected: %s", resp.status)
                if txp_ok:
                    _LOGGER.debug(
                        f"Note: Active TxProfile applied, but TxDefaultProfile was rejected ({resp.status})."
                    )
        except Exception as ex:
            _LOGGER.debug("Set TxDefaultProfile failed: %s", ex)
            if txp_ok:
                _LOGGER.debug(
                    f"Note: Active TxProfile applied, but TxDefaultProfile failed: {ex}"
                )

        return bool(txp_ok or txd_ok)

    async def set_availability(self, state: bool = True, connector_id: int | None = 0):
        """Change availability."""
        try:
            conn = 0 if connector_id in (None, 0) else int(connector_id)
        except Exception:
            conn = 0

        typ = AvailabilityType.operative if state else AvailabilityType.inoperative
        req = call.ChangeAvailability(connector_id=conn, type=typ)

        try:
            resp = await self.call(req)
        except TimeoutError as ex:
            _LOGGER.debug("ChangeAvailability timed out (conn=%s): %s", conn, ex)
            return False
        except Exception as ex:
            _LOGGER.debug("ChangeAvailability failed (conn=%s): %s", conn, ex)
            return False

        try:
            status = getattr(resp, "status", None)

            # Fallback: some single-connector chargers reject station-level (connectorId=0).
            if status == AvailabilityStatus.rejected and conn == 0:
                try:
                    n = int(getattr(self, "num_connectors", 1) or 1)
                except Exception:
                    n = 1
                if n == 1:
                    _LOGGER.debug(
                        "Station-level ChangeAvailability rejected; retrying on connector 1."
                    )
                    return await self.set_availability(state=state, connector_id=1)

            pending_key = "availability_pending"
            target_str = "Operative" if state else "Inoperative"
            scope_str = "station" if conn == 0 else "connector"

            metric_key = (conn, cstat.status_connector.value)
            metric = self._metrics.get(metric_key)

            if status == AvailabilityStatus.scheduled:
                info = {
                    "target": target_str,
                    "scope": scope_str,
                    "since": datetime.now(tz=UTC).isoformat(),
                }
                if metric is not None:
                    metric.extra_attr[pending_key] = info

                self.hass.async_create_task(self.update(self.settings.cpid))
                return True

            if status == AvailabilityStatus.accepted:
                if metric is not None:
                    metric.extra_attr.pop(pending_key, None)
                self.hass.async_create_task(self.update(self.settings.cpid))
                return True

            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

        except Exception:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Set availability failed with response {resp.status}"
            )
            return False

    async def start_transaction(self, connector_id: int = 1):
        """Remote start a transaction."""
        _LOGGER.info("Start transaction with remote ID tag: %s", self._remote_id_tag)
        req = call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=self._remote_id_tag
        )
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Start transaction failed with response {resp.status}"
            )
            return False

    async def stop_transaction(self, connector_id: int | None = None):
        """Request remote stop of current transaction.

        If connector_id is provided, only stop the transaction running on that connector.
        """
        # Resolve which transaction to stop
        tx_id = 0
        if connector_id is not None:
            # Per-connector stop: do NOT fall back to other connectors
            try:
                tx_id = int(self._active_tx.get(int(connector_id), 0) or 0)
            except Exception:
                tx_id = 0

            # For single-connector chargers, maintain compatibility with legacy global field
            if tx_id == 0:
                try:
                    n = int(getattr(self, "num_connectors", 0) or 0)
                except Exception:
                    n = 0
                if n == 1 and int(connector_id) in (0, 1):
                    tx_id = int(self.active_transaction_id or 0)
        else:
            # Global stop (legacy behavior): stop the known active tx, or any active tx
            tx_id = int(self.active_transaction_id or 0)
            if tx_id == 0:
                tx_id = next((int(v) for v in self._active_tx.values() if v), 0)

        # Nothing to stop - succeed as no-op
        if tx_id == 0:
            return True

        req = call.RemoteStopTransaction(transaction_id=tx_id)
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True

        _LOGGER.warning("Failed with response: %s", resp.status)
        await self.notify_ha(
            f"Warning: Stop transaction failed with response {resp.status}"
        )
        return False

    async def reset(self, typ: str = ResetType.hard):
        """Hard reset charger unless soft reset requested."""
        self._metrics[0][cstat.reconnects.value].value = 0
        req = call.Reset(typ)
        resp = await self.call(req)
        if resp.status == ResetStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Reset failed with response {resp.status}")
            return False

    async def unlock(self, connector_id: int = 1):
        """Unlock charger if requested."""
        req = call.UnlockConnector(connector_id)
        resp = await self.call(req)
        if resp.status == UnlockStatus.unlocked:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Unlock failed with response {resp.status}")
            return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available.

        - firmware_url: http/https URL of the new firmware
        - wait_time: hours from now to wait before install
        """
        features = int(self.supported_features or 0)
        if not (features & prof.FW):
            _LOGGER.warning("Charger does not support OCPP firmware updating")
            return False

        schema = vol.Schema(vol.Url())
        try:
            url = schema(firmware_url)
        except vol.MultipleInvalid as e:
            _LOGGER.warning("Failed to parse url: %s", e)
            return False

        try:
            retrieve_time = (
                datetime.now(tz=UTC) + timedelta(hours=max(0, int(wait_time or 0)))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            retrieve_time = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            req = call.UpdateFirmware(location=str(url), retrieve_date=retrieve_time)
            resp = await self.call(req)
            _LOGGER.info("UpdateFirmware response: %s", resp)
            return True
        except Exception as e:
            _LOGGER.error("UpdateFirmware failed: %s", e)
            return False

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        features = int(self.supported_features or 0)
        if features & prof.FW:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(upload_url)
            except vol.MultipleInvalid as e:
                _LOGGER.warning("Failed to parse url: %s", e)
                return
            req = call.GetDiagnostics(location=str(url))
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.debug(
                "Charger %s does not support ocpp diagnostics uploading",
                self.id,
            )
            return False

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        req = call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data)
        resp = await self.call(req)
        if resp.status == DataTransferStatus.accepted:
            _LOGGER.info(
                "Data transfer [vendorId(%s), messageId(%s), data(%s)] response: %s",
                vendor_id,
                message_id,
                data,
                resp.data,
            )
            self._metrics[0][cdet.data_response.value].value = datetime.now(tz=UTC)
            self._metrics[0][cdet.data_response.value].extra_attr = {
                message_id: resp.data
            }
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Data transfer failed with response {resp.status}"
            )
            return False

    async def get_configuration(self, key: str = "") -> str | dict | None:
        """Get Configuration of charger for supported keys.

        When key is empty, returns a dict of all configuration key-value pairs.
        When key is specified, returns the value as a string.
        """
        if key == "":
            req = call.GetConfiguration()
        else:
            req = call.GetConfiguration(key=[key])
        resp = await self.call(req)
        if resp.configuration_key:
            if key == "":
                result = {}
                for entry in resp.configuration_key:
                    entry_key = entry.get("key", "")
                    entry_value = entry.get(om.value.value, "")
                    result[entry_key] = entry_value
                _LOGGER.debug("Get Configuration returned %d keys", len(result))
                return result
            value = resp.configuration_key[0][om.value.value]
            _LOGGER.debug("Get Configuration for %s: %s", key, value)
            self._metrics[0][cdet.config_response.value].value = datetime.now(tz=UTC)
            self._metrics[0][cdet.config_response.value].extra_attr = {key: value}
            return value
        if resp.unknown_key:
            _LOGGER.warning("Get Configuration returned unknown key for: %s", key)
            await self.notify_ha(f"Warning: charger reports {key} is unknown")
            return "Unknown"

    async def configure(self, key: str, value: str):
        """Configure charger by setting the key to target value.

        First the configuration key is read using GetConfiguration. The key's
        value is compared with the target value. If the key is already set to
        the correct value nothing is done.

        If the key has a different value a ChangeConfiguration request is issued.

        """
        req = call.GetConfiguration(key=[key])

        resp = await self.call(req)

        if resp.unknown_key is not None:
            if key in resp.unknown_key:
                _LOGGER.warning("%s is unknown (not supported)", key)
                return "Unknown"

        for key_value in resp.configuration_key:
            # If the key already has the targeted value we don't need to set
            # it.
            if key_value[om.key.value] == key and key_value[om.value.value] == value:
                return

            if key_value.get(om.readonly.name, False):
                _LOGGER.warning("%s is a read only setting", key)
                await self.notify_ha(f"Warning: {key} is read-only")

        req = call.ChangeConfiguration(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [
            ConfigurationStatus.rejected,
            ConfigurationStatus.not_supported,
        ]:
            _LOGGER.warning("%s while setting %s to %s", resp.status, key, value)
            await self.notify_ha(
                f"Warning: charger reported {resp.status} while setting {key}={value}"
            )
            return resp.status

        if resp.status == ConfigurationStatus.reboot_required:
            self._requires_reboot = True
            await self.notify_ha(f"A reboot is required to apply {key}={value}")
            return SetVariableResult.reboot_required

        return SetVariableResult.accepted

    async def async_update_device_info_v16(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.settings.cpid, boot_info)
        await self.async_update_device_info(
            boot_info.get(om.charge_point_serial_number.name, None),
            boot_info.get(om.charge_point_vendor.name, None),
            boot_info.get(om.charge_point_model.name, None),
            boot_info.get(om.firmware_version.name, None),
        )

    @on(Action.meter_values)
    def on_meter_values(self, connector_id: int, meter_value: dict, **kwargs):
        """Request handler for MeterValues Calls (multi-connector aware)."""

        transaction_id: int = int(kwargs.get(om.transaction_id.name, 0) or 0)
        tx_has_id: bool = transaction_id not in (None, 0)

        # Restore missing per-connector meter_start / active_transaction_id from HA if possible.
        ms_key = (connector_id, csess.meter_start.value)
        tx_key = (connector_id, csess.transaction_id.value)
        session_key = (connector_id, csess.session_time.value)

        if self._metrics[ms_key].value is None:
            value = self.get_ha_metric(csess.meter_start.value, connector_id)
            if value is None:
                m = self._metrics.get((connector_id, DEFAULT_MEASURAND))
                value = m.value if m is not None else None
            else:
                try:
                    value = float(value)
                    _LOGGER.debug(
                        "%s[%s] was None, restored value=%s from HA.",
                        csess.meter_start.value,
                        connector_id,
                        value,
                    )
                except (ValueError, TypeError):
                    value = None
            self._metrics[ms_key].value = value

        if self._metrics[tx_key].value is None:
            value = self.get_ha_metric(csess.transaction_id.value, connector_id)
            if value is None:
                value = transaction_id if transaction_id else None
            else:
                try:
                    value = int(value)
                    _LOGGER.debug(
                        "%s[%s] was None, restored value=%s from HA.",
                        csess.transaction_id.value,
                        connector_id,
                        value,
                    )
                except (ValueError, TypeError):
                    value = None
            self._metrics[tx_key].value = value
            # Track active tx per connector
            self._active_tx[connector_id] = value

        if connector_id not in self._active_tx:
            try:
                self._active_tx[connector_id] = int(self._metrics[tx_key].value or 0)
            except Exception:
                self._active_tx[connector_id] = 0

        recorded_tx = int(self._metrics[tx_key].value or 0)
        active_tx = int(self._active_tx.get(connector_id, 0) or 0)

        # Self-heal after restart: adopt incoming txId if we have none recorded yet
        if transaction_id and (recorded_tx == 0 and active_tx == 0):
            self._metrics[tx_key].value = transaction_id
            self._active_tx[connector_id] = transaction_id
            active_tx = transaction_id
            recorded_tx = transaction_id
            _LOGGER.debug(
                "Restored transactionId=%s on conn %s from MeterValues.",
                transaction_id,
                connector_id,
            )

        # Keep legacy field synced for single-connector chargers,
        # even if self-heal did not run (e.g., values were already restored).
        try:
            n_con = int(getattr(self, "num_connectors", 1) or 1)
        except Exception:
            n_con = 1
        if n_con == 1:
            try:
                legacy = int(getattr(self, "active_transaction_id", 0) or 0)
            except Exception:
                legacy = 0
            if legacy != int(active_tx or 0):
                self.active_transaction_id = int(active_tx or 0)

        transaction_matches: bool = False
        # Match is also false if no transaction is in progress, i.e. active_tx==transaction_id==0
        if transaction_id == active_tx and transaction_id != 0:
            transaction_matches = True
        elif transaction_id != 0 and active_tx != 0 and transaction_id != active_tx:
            _LOGGER.warning(
                "Unknown transaction detected on conn %s with id=%i (expected %s)",
                connector_id,
                transaction_id,
                active_tx,
            )

        meter_values: list[list[MeasurandValue]] = []
        for bucket in meter_value:
            measurands: list[MeasurandValue] = []
            for sampled_value in bucket.get(om.sampled_value.name, []):
                measurand = sampled_value.get(om.measurand.value, None)
                value = sampled_value.get(om.value.value, None)
                # Where an empty string is supplied convert to 0
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    value = 0.0
                unit = sampled_value.get(om.unit.value, None)
                phase = sampled_value.get(om.phase.value, None)
                location = sampled_value.get(om.location.value, None)
                context = sampled_value.get(om.context.value, None)
                measurands.append(
                    MeasurandValue(measurand, value, phase, unit, context, location)
                )
            meter_values.append(measurands)

        self.process_measurands(meter_values, transaction_matches, connector_id)

        if tx_has_id and transaction_matches:
            # session_time is stored as the unix epoch of the transaction start.
            # Guard: only treat the stored value as a timestamp if it looks like one
            # (> year 2000 = 946684800). The transaction_id (e.g. 1, 2) must not be
            # used as a timestamp — that would produce ~56 years of session time.
            try:
                stored = self._metrics[session_key].value
                if stored is not None and float(stored) > 946684800:
                    tx_start_epoch = float(stored)
                else:
                    tx_start_epoch = None
            except (TypeError, ValueError):
                tx_start_epoch = None
            if tx_start_epoch is not None:
                self._metrics[session_key].value = round(
                    (time.time() - tx_start_epoch) / 60
                )
                self._metrics[session_key].unit = UnitOfTime.MINUTES
            else:
                _LOGGER.debug(
                    "Skipping session time calc — no valid tx_start_epoch stored",
                )
        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.MeterValues()

    @on(Action.boot_notification)
    def on_boot_notification(self, **kwargs):
        """Handle a boot notification."""
        resp = call_result.BootNotification(
            current_time=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            interval=3600,
            status=RegistrationStatus.accepted.value,
        )
        self.received_boot_notification = True
        _LOGGER.debug("Received boot notification for %s: %s", self.id, kwargs)

        self.hass.async_create_task(self.async_update_device_info_v16(kwargs))
        self._register_boot_notification()
        return resp

    @on(Action.status_notification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle a status notification."""

        if connector_id == 0 or connector_id is None:
            self._metrics[(0, cstat.status.value)].value = status
            self._metrics[(0, cstat.error_code.value)].value = error_code
        else:
            self._metrics[(connector_id, cstat.status_connector.value)].value = status
            self._metrics[
                (connector_id, cstat.error_code_connector.value)
            ].value = error_code

            if status in (
                ChargePointStatus.suspended_ev.value,
                ChargePointStatus.suspended_evse.value,
            ):
                for meas in [
                    Measurand.current_import.value,
                    Measurand.power_active_import.value,
                    Measurand.power_reactive_import.value,
                    Measurand.current_export.value,
                    Measurand.power_active_export.value,
                    Measurand.power_reactive_export.value,
                ]:
                    if meas in self._metrics[connector_id]:
                        self._metrics[(connector_id, meas)].value = 0

        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StatusNotification()

    @on(Action.firmware_status_notification)
    def on_firmware_status(self, status, **kwargs):
        """Handle firmware status notification."""
        self._metrics[0][cstat.firmware_status.value].value = status
        self.hass.async_create_task(self.update(self.settings.cpid))
        self.hass.async_create_task(self.notify_ha(f"Firmware upload status: {status}"))
        return call_result.FirmwareStatusNotification()

    @on(Action.diagnostics_status_notification)
    def on_diagnostics_status(self, status, **kwargs):
        """Handle diagnostics status notification."""
        _LOGGER.info("Diagnostics upload status: %s", status)
        self.hass.async_create_task(
            self.notify_ha(f"Diagnostics upload status: {status}")
        )
        return call_result.DiagnosticsStatusNotification()

    @on(Action.security_event_notification)
    def on_security_event(self, type, timestamp, **kwargs):
        """Handle security event notification."""
        _LOGGER.info(
            "Security event notification received: %s at %s [techinfo: %s]",
            type,
            timestamp,
            kwargs.get(om.tech_info.name, "none"),
        )
        self.hass.async_create_task(
            self.notify_ha(f"Security event notification received: {type}")
        )
        return call_result.SecurityEventNotification()

    @on(Action.authorize)
    def on_authorize(self, id_tag, **kwargs):
        """Handle an Authorization request."""
        self._metrics[0][cstat.id_tag.value].value = id_tag
        auth_status = self.get_authorization_status(id_tag)
        return call_result.Authorize(id_tag_info={om.status.value: auth_status})

    @on(Action.start_transaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        """Handle a Start Transaction request."""

        auth_status = self.get_authorization_status(id_tag)
        if auth_status == AuthorizationStatus.accepted.value:
            tx_id = int(time.time())
            self._active_tx[connector_id] = tx_id
            self.active_transaction_id = tx_id
            self._metrics[(connector_id, cstat.id_tag.value)].value = id_tag
            self._metrics[(connector_id, cstat.stop_reason.value)].value = ""
            self._metrics[(connector_id, csess.transaction_id.value)].value = tx_id
            try:
                meter_start_kwh = float(meter_start) / 1000.0
            except Exception:
                meter_start_kwh = 0.0
            self._metrics[
                (connector_id, csess.meter_start.value)
            ].value = meter_start_kwh
            self._metrics[(connector_id, csess.meter_start.value)].unit = HA_ENERGY_UNIT

            self._metrics[(connector_id, csess.session_time.value)].value = 0
            self._metrics[
                (connector_id, csess.session_time.value)
            ].unit = UnitOfTime.MINUTES
            self._metrics[(connector_id, csess.session_energy.value)].value = 0.0
            self._metrics[
                (connector_id, csess.session_energy.value)
            ].unit = HA_ENERGY_UNIT

            result = call_result.StartTransaction(
                id_tag_info={om.status.value: AuthorizationStatus.accepted.value},
                transaction_id=tx_id,
            )
        else:
            result = call_result.StartTransaction(
                id_tag_info={om.status.value: auth_status},
                transaction_id=0,
            )

        self.hass.async_create_task(self.update(self.settings.cpid))
        return result

    @on(Action.stop_transaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Stop the current transaction (multi-connector)."""

        # Resolve connector from active tx map
        conn = next(
            (c for c, tx in self._active_tx.items() if tx == transaction_id), None
        )
        if conn is None:
            _LOGGER.error(
                "Stop transaction received for unknown transaction id=%i",
                transaction_id,
            )
            conn = 1  # conservative fallback

        # Reset active transaction (global + per-connector)
        self._active_tx[conn] = 0
        self.active_transaction_id = 0
        self._metrics[(conn, cstat.id_tag.value)].value = ""
        self._metrics[(conn, csess.transaction_id.value)].value = 0
        self._metrics[(conn, cstat.stop_reason.value)].value = kwargs.get(
            om.reason.name, None
        )

        ms_key = (conn, csess.meter_start.value)
        if (
            self._metrics[ms_key].value is not None
            and not self._charger_reports_session_energy
        ):
            try:
                session_kwh = int(meter_stop) / 1000.0 - float(
                    self._metrics[ms_key].value
                )
            except Exception:
                session_kwh = 0.0
            self._metrics[(conn, csess.session_energy.value)].value = session_kwh

        for meas in [
            Measurand.current_import.value,
            Measurand.power_active_import.value,
            Measurand.power_reactive_import.value,
            Measurand.current_export.value,
            Measurand.power_active_export.value,
            Measurand.power_reactive_export.value,
        ]:
            key = (conn, meas)
            if key in self._metrics:
                self._metrics[key].value = 0

        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StopTransaction(
            id_tag_info={om.status.value: AuthorizationStatus.accepted.value}
        )

    @staticmethod
    def _tokenize_evbox_data(data: str) -> list[str]:
        """Tokenize EVBox CSV data string that may contain {group,values}."""
        tokens: list[str] = []
        current = ""
        depth = 0
        for ch in data:
            if ch == "{":
                depth += 1
                current += ch
            elif ch == "}":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                tokens.append(current)
                current = ""
            else:
                current += ch
        if current:
            tokens.append(current)
        return tokens

    def _parse_evbox_status_notification(self, data: str, connector_id: int) -> None:
        """Parse EVBox evbStatusNotification data string and update HA metrics.

        Format — 25 top-level tokens (commas inside {…} do not split):
          tok[0]  connectorId
          tok[1]  status              Available|Charging|Preparing|SuspendedEV(SE)
          tok[2]  errorCode
          tok[3]  info
          tok[4]  vendorErrorCode     1=session active, 0=idle
          tok[5]  ledColor
          tok[6]  ledOn
          tok[7]  {hwMaxCurrentA, maxPowerW, dutyPct}
                    hwMaxCurrentA  = charger hardware max A (32 for this unit); 0 when no car
                    maxPowerW      = rated max power W (~4800)
                    dutyPct        = CP PWM duty cycle % (13%≈8A, 26%≈16A, 53%≈32A, 100%=no car)
                    offered_current = duty * 0.6  (valid for 10–85%)
          tok[8]  {lifetimeEnergyWh, sessionEnergyWh}
          tok[9]  {pilotStateChar, supplyMV, peakPosMV, peakNegMV}
                    pilotStateChar = IEC 61851: A≈12V no car, B≈9V connected, C≈6V charging
                    peakPosMV      confirms state: ~12000=A, ~9000=B, ~6000=C
          tok[10] unknown1
          tok[11] unknown2
          tok[12] gridVoltage_V       L-L voltage (380–410 V)
          tok[13] timestamp           ISO-8601 UTC
          tok[14] transactionId       0 = no active session
          tok[15] firmwareDiscriminator
                    > 3  → new firmware (W7.x): value is WiFi RSSI magnitude (e.g. 78 → -78 dBm)
                    ≤ 3  → old firmware (W6.x): value is number of phases; use pilot ASCII for RSSI
          tok[16] meterGroup {L1_V, tempC, unk, currentDa, 0, 0, pfX1000, 0, 0}  [new FW only]
                    [0] L1 phase voltage V (sags under load: 234V idle → 218V at 32A)
                    [1] internal temperature °C (tentative — drops when cooling fan runs)
                    [3] measured current dA  (÷10 → A;  73=7.3A, 152=15.2A, 310=31.0A confirmed)
                    [6] power factor × 1000  (e.g. 999 → 0.999)
          tok[17] unknown3            varies: 270 idle, 300–310 charging
          tok[18] sessionDuration_min
          tok[19] cellularSignalBars  0–5
          tok[20] internalParam       slowly drifting, identity unknown
          tok[21] unknown5
          tok[22] clockAlignedInterval_s
          tok[23] ocppCurrentLimit_dA  ONLY valid when tok[14]>0; else HeartbeatInterval
          tok[24] firmwareParam        constant 5004
        """
        tokens = self._tokenize_evbox_data(data)
        if len(tokens) < 16:
            _LOGGER.debug(
                "EVBox evbStatusNotification has only %d tokens, expected >=16; skipping",
                len(tokens),
            )
            return

        try:
            status = tokens[1]
            error_code = tokens[2]
            led_color = tokens[5] if len(tokens) > 5 else None
            lock_raw = tokens[4] if len(tokens) > 4 else None
            try:
                lock_status = int(lock_raw)
            except (TypeError, ValueError):
                lock_status = lock_raw

            # tok[7]: {hwMaxCurrentA, maxPowerW, dutyPct}
            power_parts = tokens[7].strip("{}").split(",")
            hw_max_current_a = float(power_parts[0]) if len(power_parts) > 0 else None  # hardware rated max (32A constant; 0 when no car)
            max_power_w = float(power_parts[1]) if len(power_parts) > 1 else None        # rated max power W
            duty_cycle_pct = float(power_parts[2]) if len(power_parts) > 2 else None     # CP PWM duty cycle %
            # Offered current from duty cycle (IEC 61851, valid for 10–85%)
            offered_current_a = (
                duty_cycle_pct * 0.6
                if duty_cycle_pct is not None and 10 <= duty_cycle_pct <= 85
                else None
            )

            # tok[8]: {lifetimeEnergyWh, sessionEnergyWh}
            energy_parts = tokens[8].strip("{}").split(",")
            total_energy_wh = float(energy_parts[0]) if len(energy_parts) > 0 else None
            session_energy_wh = float(energy_parts[1]) if len(energy_parts) > 1 else None

            # tok[9]: {pilotStateChar, supplyMV, peakPosMV, peakNegMV}
            _t9_parts = tokens[9].strip("{}").split(",")
            pilot_state_char = None
            if _t9_parts:
                _first = _t9_parts[0].strip()
                if _first and _first[0].isalpha():
                    pilot_state_char = _first[0].upper()  # A=no car, B=connected, C=charging

            # tok[15]: firmware discriminator
            # > 3  → new firmware (W7.x): value is WiFi RSSI magnitude, tok[16] meter group valid
            # ≤ 3  → old firmware (W6.x): value is number of phases; pilot char ASCII ≈ RSSI
            fw_disc = None
            try:
                fw_disc = int(tokens[15])
            except (TypeError, ValueError, IndexError):
                pass
            is_new_firmware = fw_disc is not None and fw_disc > 3
            if is_new_firmware:
                wifi_rssi_dbm = -fw_disc  # e.g. 78 → -78 dBm
            elif pilot_state_char is not None:
                wifi_rssi_dbm = -ord(pilot_state_char)  # old FW: 'A'=65 → -65 dBm
            else:
                wifi_rssi_dbm = None

            # tok[14]: transactionId (0 = no active session)
            transaction_id_evb = None
            try:
                transaction_id_evb = int(tokens[14]) if len(tokens) > 14 else None
            except (TypeError, ValueError):
                pass

            # tok[23]: OCPP current limit in dA — only valid when a transaction is active
            ocpp_limit_a = None
            if len(tokens) > 23 and transaction_id_evb:
                try:
                    ocpp_limit_a = float(tokens[23]) / 10.0
                except (TypeError, ValueError):
                    pass

            # tok[19]: cellular signal bars 0–5
            cellular_bars = None
            try:
                cellular_bars = int(tokens[19]) if len(tokens) > 19 else None
            except (TypeError, ValueError):
                pass

            # tok[16]: meter group {L1_V, tempC, unk, currentDa, 0, 0, pfX1000, 0, 0}
            # Only meaningful on new firmware; on old firmware tok[16] may not exist.
            voltage_v = None
            internal_temp_c = None
            current_l1_da = None
            current_l2_da = None
            current_l3_da = None
            power_factor = None
            if len(tokens) > 16:
                meter_parts = tokens[16].strip("{}").split(",")
                try:
                    voltage_v = float(meter_parts[0])
                except (ValueError, IndexError):
                    pass
                try:
                    internal_temp_c = float(meter_parts[1])
                except (ValueError, IndexError):
                    pass
                try:
                    current_l1_da = float(meter_parts[3])
                except (ValueError, IndexError):
                    pass
                try:
                    current_l2_da = float(meter_parts[4])
                except (ValueError, IndexError):
                    pass
                try:
                    current_l3_da = float(meter_parts[5])
                except (ValueError, IndexError):
                    pass
                try:
                    pf_raw = float(meter_parts[6])
                    power_factor = pf_raw / 1000.0 if pf_raw > 0 else None
                except (ValueError, IndexError):
                    pass

            # Calculate active power: V × I × PF
            active_power_w = None
            if voltage_v is not None and current_l1_da is not None:
                measured_a = current_l1_da / 10.0
                pf = power_factor if power_factor is not None else 1.0
                active_power_w = voltage_v * measured_a * pf

            cid = connector_id

            # --- Status & error --------------------------------------------------
            self._metrics[(cid, cstat.status_connector.value)].value = status
            self._metrics[(cid, cstat.error_code_connector.value)].value = error_code

            # --- Active power: V × I × PF → kW ----------------------------------
            if active_power_w is not None:
                self._metrics[(cid, Measurand.power_active_import.value)].value = (
                    active_power_w / 1000.0
                )
                self._metrics[(cid, Measurand.power_active_import.value)].unit = (
                    HA_POWER_UNIT
                )

            # --- Total lifetime energy: Wh → kWh ---------------------------------
            if total_energy_wh is not None:
                self._metrics[
                    (cid, Measurand.energy_active_import_register.value)
                ].value = total_energy_wh / 1000.0
                self._metrics[
                    (cid, Measurand.energy_active_import_register.value)
                ].unit = HA_ENERGY_UNIT

            # --- Session energy: Wh → kWh ----------------------------------------
            if session_energy_wh is not None:
                self._metrics[(cid, csess.session_energy.value)].value = (
                    session_energy_wh / 1000.0
                )
                self._metrics[(cid, csess.session_energy.value)].unit = HA_ENERGY_UNIT

            # --- Per-phase current: dA → A ----------------------------------------
            if any(v is not None for v in [current_l1_da, current_l2_da, current_l3_da]):
                l1_a = (current_l1_da or 0.0) / 10.0
                l2_a = (current_l2_da or 0.0) / 10.0
                l3_a = (current_l3_da or 0.0) / 10.0
                max_measured = max(l1_a, l2_a, l3_a)
                self._metrics[(cid, Measurand.current_import.value)].value = max_measured
                self._metrics[(cid, Measurand.current_import.value)].unit = "A"
                self._metrics[(cid, Measurand.current_import.value)].extra_attr = {
                    Phase.l1.value: l1_a,
                    Phase.l2.value: l2_a,
                    Phase.l3.value: l3_a,
                }

            # --- Voltage: L1-N phase voltage V ------------------------------------
            if voltage_v is not None:
                self._metrics[(cid, Measurand.voltage.value)].value = voltage_v
                self._metrics[(cid, Measurand.voltage.value)].unit = "V"

            # --- Power factor (meter group [6] / 1000) ----------------------------
            if power_factor is not None:
                self._metrics[(cid, Measurand.power_factor.value)].value = power_factor
                self._metrics[(cid, Measurand.power_factor.value)].unit = ""

            # --- Internal temperature °C (meter group [1], tentative) ------------
            if internal_temp_c is not None:
                self._metrics[(cid, Measurand.temperature.value)].value = internal_temp_c
                self._metrics[(cid, Measurand.temperature.value)].unit = "celsius"

            # --- EVBox-specific sensors (connector-level) -------------------------
            self._metrics[(cid, evbox.lock_status.value)].value = lock_status
            vehicle_connected = (
                pilot_state_char in ("B", "C") if pilot_state_char is not None else None
            )
            self._metrics[(cid, evbox.vehicle_connected.value)].value = vehicle_connected
            # Hardware rated max current from tok[7][0] (constant 32A; 0 when no car)
            if hw_max_current_a is not None:
                self._metrics[(cid, evbox.max_current.value)].value = hw_max_current_a
                self._metrics[(cid, evbox.max_current.value)].unit = "A"
            # OCPP current limit from tok[23] — only valid during active session
            if ocpp_limit_a is not None:
                self._metrics[(cid, evbox.smart_limit.value)].value = ocpp_limit_a
                self._metrics[(cid, evbox.smart_limit.value)].unit = "A"
            elif not transaction_id_evb:
                self._metrics[(cid, evbox.smart_limit.value)].value = None
            if pilot_state_char is not None:
                self._metrics[(cid, evbox.pilot_state.value)].value = pilot_state_char
            if duty_cycle_pct is not None:
                self._metrics[(cid, evbox.cp_duty_cycle.value)].value = duty_cycle_pct
                self._metrics[(cid, evbox.cp_duty_cycle.value)].unit = "%"

            # --- EVBox-specific sensors (charger-level) ---------------------------
            self._metrics[0][evbox.led_color.value].value = led_color
            if wifi_rssi_dbm is not None:
                self._metrics[0][evbox.signal_strength.value].value = wifi_rssi_dbm
                self._metrics[0][evbox.signal_strength.value].unit = "dBm"
            if max_power_w is not None:
                self._metrics[0][evbox.max_power.value].value = max_power_w / 1000.0
                self._metrics[0][evbox.max_power.value].unit = "kW"
            if cellular_bars is not None:
                self._metrics[0][evbox.cellular_bars.value].value = cellular_bars

            # --- Summary attributes on the data_transfer timestamp sensor ---------
            self._metrics[0][cdet.data_transfer.value].extra_attr = {
                "evbox_status": status,
                "evbox_error_code": error_code,
                "evbox_led_color": led_color,
                "evbox_lock_status": lock_status,
                "evbox_pilot_state": pilot_state_char,
                "evbox_wifi_rssi_dbm": wifi_rssi_dbm,
                "evbox_firmware": "new" if is_new_firmware else "old",
                "evbox_cp_duty_cycle_pct": duty_cycle_pct,
                "evbox_offered_current_a": offered_current_a,
                "evbox_hw_max_current_a": hw_max_current_a,
                "evbox_ocpp_limit_a": ocpp_limit_a,
                "evbox_max_power_kw": (max_power_w / 1000.0) if max_power_w is not None else None,
                "evbox_power_factor": power_factor,
                "evbox_active_power_w": active_power_w,
                "evbox_cellular_bars": cellular_bars,
                "evbox_transaction_id": transaction_id_evb,
                "evbox_connector_id": cid,
            }

            _l1 = (current_l1_da or 0.0) / 10.0 if current_l1_da is not None else None
            _LOGGER.debug(
                "EVBox %s conn %s: status=%s pilot=%s fw=%s rssi=%sdBm"
                " power=%.1fW (V=%.1f I=%.2fA PF=%.3f) duty=%.0f%% offered=%.1fA limit=%sA"
                " energy=%.3fkWh session=%.3fkWh temp=%s°C cellular=%s",
                self.id,
                cid,
                status,
                pilot_state_char,
                "new" if is_new_firmware else "old",
                wifi_rssi_dbm,
                active_power_w or 0,
                voltage_v or 0,
                _l1 or 0,
                power_factor or 0,
                duty_cycle_pct or 0,
                offered_current_a or 0,
                ocpp_limit_a,
                (total_energy_wh or 0) / 1000.0,
                (session_energy_wh or 0) / 1000.0,
                internal_temp_c,
                cellular_bars,
            )

        except Exception as ex:
            _LOGGER.warning(
                "Failed to parse EVBox evbStatusNotification: %s",
                ex,
                exc_info=True,
            )

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Handle a Data transfer request."""
        _LOGGER.debug("Data transfer received from %s: %s", self.id, kwargs)
        self._metrics[0][cdet.data_transfer.value].value = datetime.now(tz=UTC)
        self._metrics[0][cdet.data_transfer.value].extra_attr = {vendor_id: kwargs}

        # Handle EVBox vendor-specific messages
        if vendor_id == "EV-BOX":
            message_id = kwargs.get("message_id", "")
            data = kwargs.get("data", "")
            if message_id == "evbStatusNotification" and data:
                try:
                    raw_connector = int(self._tokenize_evbox_data(data)[0])
                except Exception:
                    raw_connector = 1
                self._parse_evbox_status_notification(data, raw_connector)
                self.hass.async_create_task(self.update(self.settings.cpid))

        return call_result.DataTransfer(status=DataTransferStatus.accepted.value)

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle a Heartbeat."""
        now = datetime.now(tz=UTC)
        self._metrics[0][cstat.heartbeat.value].value = now
        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.Heartbeat(current_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
