#!/usr/bin/env python3
"""
acf_mapper.py  —  Map zigwheels.ae (UAE) scraped JSON → WordPress ACF payload

ACF Structure (as of 2026-06-15 export):
  cars-data post type:
    group_6a2952b35c81d  Cars Overview       → cars_gallery, launched_date,
                                               price_range, body_type, fuel_type,
                                               transmission, mileage
    group_6a29555155e67  Key Specifications   → engine_cc, max_torque, no_of_airbags,
                                               bootspace, ground_clearance,
                                               fuel_tank_capacity, seating_capacity,
                                               drive_type
    group_6a29561a24983  Latest Updates       → latest_updates_list (repeater)
    group_6a29578691443  What's New Content   → intro/exterior/interior/powertrain

  car-variant post type:
    group_6a2a3bd25d238  Specifications       → repeaters per category
    group_6a2a3bd4f1ae6  Features             → repeaters per category

FIX LOG (2026-06-15):
  Bug 1 — Repeater rows must use sub-field *names* ("spec_name", "spec_value"),
           not field keys. ACF REST API matches rows by sub-field name.

  Bug 2 — tyres_and_wheels ACF repeater in WP has lighting choices (copy-paste error
           in the field group). SPEC_TO_TYRES_CHOICE keys never matched any valid
           WP choice, causing silent drops.
           Fixed: added correct tyre choice keys to SPEC_TO_TYRES_CHOICE (you must
           also add matching choices in WP ACF admin — see comment in that dict).

  Bug 3 — Model + variant ACF payloads must use field *names* (max_torque,
           comfort_and_convenience, etc.), not field keys (field_6a…).
           ACF REST API only accepts names unless admin-mode field keys are used.

  Bug 4 — ACF field groups must have "Show in REST API" enabled in WordPress.
           Without it the REST API returns "acf": {} and silently ignores writes.
           Re-import acf-export-2026-06-15-rest-enabled.json to fix this.

FIX LOG (2026-08-12):
  Bug 5 — "Hero" content came through empty. price_overview_text / hero
           post_content depends on scrapper._extract_overview_text(), which
           only followed a strict <p> sibling chain and broke on this site's
           div-wrapped "Read More/Read Less" intro block. Fixed in
           scrapper.py (now walks leaf-level p/div/span nodes) — see that
           file's own 2026-08-12 fix note.

  Bug 6 — Latest Updates ACF group was always empty even though
           LATEST_UPDATES_REPEATER/HEADLINE/DATE field keys were defined —
           they were never actually used anywhere. Added
           _build_latest_updates_acf() and wired it into map_model_to_acf().

  Bug 7 — What's New only ever filled introduction_list (from the Updates
           timeline, which belongs in Latest Updates, not here) and left
           exterior_list/interior_list/powertrain_list permanently empty.
           _build_whats_new_acf() now sources from
           model["review_narrative"] (scrapper._extract_review_narrative —
           the Review accordion's Summary/Interior/Exterior/Engine & Fuel
           Consumption tabs), routed through the existing
           WHATS_NEW_REPEATER_MAP.

  Bug 8 — Specifications repeaters came through almost empty (Features
           still worked). CATEGORY_TO_GROUP and the per-group "choices"
           dicts were matched by exact string only, but real scraped
           categories/spec names are free-form review prose ("Core
           Dimensions:", "Front Headroom") rather than the tidy labels
           these dicts were written against. Added fuzzy matching
           (_match_category_group, expanded _lookup_choice) plus several
           genuinely-missing choice keys (engine/dimensions/suspension/
           ADAS) — NOTE: the new choice keys marked "NEW" in the dicts
           below are inert until you add matching choices to the
           corresponding ACF select field in WP Admin; see the field-by-
           field list in chat / commit notes.
"""

import argparse
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Spec name → ACF select choice KEY used inside repeater sub-fields
# These are the "value" keys of the select fields (not field_6a… IDs).
# ---------------------------------------------------------------------------

# Engine & Performance repeater (field_6a2a3bcd9b778)
SPEC_TO_ENGINE_CHOICE: dict[str, str] = {
    "engine type":                      "engine_type",
    "displacement":                     "displacement_cc",
    "displacement (cc)":                "displacement_cc",
    "engine displacement":              "displacement_cc",
    "no. of cylinders":                 "no_of_cylinders",
    "number of cylinders":              "no_of_cylinders",
    "max power":                        "max_power",
    "power":                            "power",
    "max torque":                       "max_torque",
    "torque":                           "torque",
    "top speed":                        "top_speed",
    "acceleration":                     "acceleration_0_to_100_kmph",
    "0-100 kmph":                       "acceleration_0_to_100_kmph",
    "0 to 100":                         "acceleration_0_to_100_kmph",
    "no. of gears":                     "no_of_gears",
    "number of gears":                  "no_of_gears",
    "gear box":                         "no_of_gears",
    "gearbox":                          "no_of_gears",
    "drive type":                       "drive_train",
    "drive train":                      "drive_train",
    "drivetrain":                       "drivetrain",
    "turbocharger":                     "turbocharger",
    "supercharger":                     "turbocharger",
    "emission standard":                "emissions_standard",
    "emissions standard":               "emissions_standard",
    "motor type":                       "motor_type",
    "electric motor":                   "motor_type",
    "motor power":                      "motor_power",
    # FIX (2026-08-17): hybrid/EV models phrase this with an "electric"
    # prefix that the plain "motor power"/"motor type" keys above don't
    # exactly match (SPEC_REPEATERS lookups are exact-match only — see
    # _lookup_choice's fuzzy=False note — so close-but-not-identical names
    # were silently dropped for e.g. the Elantra HEV / Tesla Model 3).
    "electric motor power":             "motor_power",
    "electric motor type":              "motor_type",
    "secondary engine type":            "motor_type",
    "secondary engine fuel type":       "fuel_supply_system",
    "combined power output":            "max_power",
    "system power":                     "max_power",
    "system power output":              "max_power",
    # --- NEW (2026-08-12): these choice KEYS do not exist in WP yet — the
    # Review-accordion "Engine Specifications" sub-section on zigwheels.ae
    # regularly includes them, but they were previously silently dropped
    # because there was nothing to match against. Add matching choices to
    # the "Engine and Performance" repeater's spec_name SELECT field in
    # WP Admin (ACF > Field Groups > Specifications) using these same
    # value slugs, THEN this mapping will start working — until you do,
    # these five lines are inert (see FEATURE MATRIX in the chat reply).
    "engine code":                      "engine_code",
    "bore x stroke":                    "bore_x_stroke",
    "compression ratio":                "compression_ratio",
    "valvetrain":                       "valvetrain",
    "variable valve timing":            "variable_valve_timing",
    # FIX (2026-08-13): mapper used to emit "fuel_delivery_system", but the
    # real WP select field's choice key (confirmed against
    # acf-export-2026-08-13.json) is "fuel_supply_system" — the mismatched
    # key meant this row silently failed to save via the ACF REST API.
    "fuel delivery":                    "fuel_supply_system",
    "fuel supply system":               "fuel_supply_system",
    "fuel delivery system":             "fuel_supply_system",
    # FIX (2026-08-13): "valves_per_cylinder" already exists as a WP choice
    # (acf-export-2026-08-13.json) but nothing in this dict ever targeted
    # it, so valve-count specs from the review accordion were dropped.
    "valves per cylinder":              "valves_per_cylinder",
    "no. of valves":                    "valves_per_cylinder",
    "number of valves":                 "valves_per_cylinder",
    "valves":                           "valves_per_cylinder",
}

# Dimensions & Weight repeater (field_6a2a4e60ae7e7)
SPEC_TO_DIMENSIONS_CHOICE: dict[str, str] = {
    "length":                           "length",
    "width":                            "width",
    "height":                           "height",
    "wheelbase":                        "wheelbase",
    "ground clearance":                 "ground_clearance",
    "kerb weight":                      "kerb_weight",
    # --- NEW (2026-08-12): needs matching WP choices added first (see note
    # in SPEC_TO_ENGINE_CHOICE above) — off-road/handling geometry figures
    # that show up in the Review accordion's "Core Dimensions" sub-section.
    "approach angle":                   "approach_angle",
    "departure angle":                  "departure_angle",
    "turning radius":                   "turning_radius",
}

# Capacity repeater (field_6a2a4e63ae7ea)
SPEC_TO_CAPACITY_CHOICE: dict[str, str] = {
    "boot space":                       "boot_space",
    "no. of doors":                     "no_of_doors",
    "number of doors":                  "no_of_doors",
    "no. of rows":                      "no_of_rows",
    "number of rows":                   "no_of_rows",
    "seating capacity":                 "seating_capacity",
    "fuel tank capacity":               "fuel_tank_capacity",
    "fuel tank":                        "fuel_tank_capacity",
}

# Fuel Efficiency repeater (field_6a2a4e64ae7ed)
SPEC_TO_FUEL_CHOICE: dict[str, str] = {
    # NOTE: these choice keys ("mileage_arai" etc.) are the WP ACF field choice
    # values already configured for this site. UAE listings don't use India's
    # ARAI test-cycle figure, so these now carry UAE-style combined/city/highway
    # fuel-consumption or EV range numbers instead — rename the choices in WP
    # admin if you want the labels to read "Fuel Consumption" instead of "ARAI".
    "arai mileage":                     "mileage_arai",
    "mileage arai":                     "mileage_arai",
    "mileage (arai)":                   "mileage_arai",
    "mileage":                          "mileage_arai",
    "fuel consumption":                 "mileage_arai",
    "combined fuel consumption":        "mileage_arai",
    "average fuel consumption":         "mileage_arai",
    "fuel efficiency":                  "mileage_arai",
    "range":                            "mileage_arai",
    "wltp range":                       "mileage_arai",
    "city mileage":                     "mileage_city",
    "mileage - city":                   "mileage_city",
    "mileage city":                     "mileage_city",
    "city fuel consumption":            "mileage_city",
    "highway mileage":                  "mileage_highway",
    "mileage - highway":                "mileage_highway",
    "mileage highway":                  "mileage_highway",
    "highway fuel consumption":         "mileage_highway",
}

# Suspension, Brakes & Steering repeater (field_6a2a4e6dae7f0)
SPEC_TO_SUSPENSION_CHOICE: dict[str, str] = {
    "front suspension":                 "front_suspension",
    "rear suspension":                  "rear_suspension",
    "front brake":                      "front_brake_type",
    "front brake type":                 "front_brake_type",
    "rear brake":                       "rear_brake_type",
    "rear brake type":                  "rear_brake_type",
    "steering type":                    "steering_type",
    "steering":                         "steering_type",
    "steering adjustability":           "steering_adjustability",
    # --- NEW (2026-08-12): needs matching WP choices added first (see note
    # in SPEC_TO_ENGINE_CHOICE above).
    "dampers":                          "dampers",
    "anti-roll bars":                   "anti_roll_bars",
}

# Tyres & Wheels repeater (field_6a2a4e71ae7f3)
# BUG 2 FIX: The WP ACF export shows this repeater currently has *lighting* choices
# (copy-paste error). To make tyre data actually save, you must add the following
# choice keys in WP Admin → ACF → Specifications → Tyres and Wheels → spec_name:
#   wheel_type       → Wheel Type
#   tyre_type        → Tyre Type
#   front_tyre_size  → Front Tyre Size
#   rear_tyre_size   → Rear Tyre Size
#   spare_wheel      → Spare Wheel
#   spare_wheel_size → Spare Wheel Size
SPEC_TO_TYRES_CHOICE: dict[str, str] = {
    "wheel type":                       "wheel_type",
    "tyre type":                        "tyre_type",
    "front tyre size":                  "front_tyre_size",
    "rear tyre size":                   "rear_tyre_size",
    # WP has these as two DISTINCT choices (confirmed against
    # acf-export-2026-08-11.json) — "spare wheel" (presence/kind, e.g.
    # "Full-size") vs "spare wheel type" (the more specific type text).
    # Previously both synonyms collapsed onto "spare_wheel", so
    # "spare_wheel_type" was never actually used.
    "spare wheel":                      "spare_wheel",
    "spare wheel type":                 "spare_wheel_type",
    "spare wheel size":                 "spare_wheel_size",
    # FIX (2026-08-13): FAQ-mined "Tyre Size" (e.g. "205/55 R16") doesn't
    # specify front vs rear — most cars use the same size on both axles, so
    # route it to both choices rather than dropping it.
    "tyre size":                        "front_tyre_size",
}
# "tyre size" also mirrors into rear_tyre_size — handled as a second pass in
# _build_spec_repeaters via TYRE_SIZE_MIRROR below (kept out of the dict
# above since a dict can only hold one value per key).
TYRE_SIZE_MIRROR = {"tyre size": "rear_tyre_size"}

# Battery, Range & Charging repeater (field_6a2bf0f2292c4)
SPEC_TO_BATTERY_CHOICE: dict[str, str] = {
    "battery type":                     "battery_type",
    "battery capacity":                 "battery_capacity",
    # FIX (2026-08-17): hybrid/EV models (e.g. Elantra HEV, Tesla Model 3)
    # phrase this a few different ways on zigwheels.ae — all the same figure,
    # so all route to the one existing "battery_capacity" WP choice.
    "battery pack capacity":            "battery_capacity",
    "usable battery capacity":          "battery_capacity",
    "net battery capacity":             "battery_capacity",
    "gross battery capacity":           "battery_capacity",
    "battery voltage":                  "battery_voltage",
    "charging time":                    "charging_time",
    "charging time (ac) fast charging": "charging_time_ac_fast_charging",
    "fast charging time":               "fast_charging_time",
    "ac fast charging":                 "ac_fast_charging",
    "dc fast charging":                 "fast_charging_time",
    "battery range":                    "battery_range",
    "range":                            "battery_range",
    "electric range":                   "battery_range",
    "driving range":                    "battery_range",
    "wltp range":                       "battery_range",
    "arai range":                       "battery_range",
}

# ---------------------------------------------------------------------------
# Features category → ACF repeater field key + sub-field keys
# ---------------------------------------------------------------------------

FEATURE_REPEATERS = {
    # Comfort & Convenience
    "comfort_and_convenience": {
        "repeater_key": "field_6a2a3bcf845cf",
        "spec_name_key": "field_6a2a4fc5749ee",
        "spec_value_key": "field_6a2a4fd6749ef",
        "choices": {
            "air conditioner":                          "air_conditioner",
            "ac":                                       "air_conditioner",
            "front ac":                                 "front_ac",
            "rear ac":                                  "rear_ac",
            "third row ac":                             "third_row_ac_zone",
            "heater":                                   "heater",
            "climate control":                          "climate_control",
            "paddle shifters":                          "paddle_shift",
            "paddle shift":                             "paddle_shift",
            "idle start/stop":                          "idle_start_stop_button",
            "start/stop button":                        "idle_start_stop_button",
            "cruise control":                           "cruise_control",
            "power windows":                            "power_windows",
            "keyless entry":                            "keyless_entry",
            "push start":                               "push_start_stop_button",
            "push button start":                        "push_start_stop_button",
            "remote engine start":                      "remote_engine_start",
            "hands-free tailgate":                      "hands_free_tailgate",
            "steering mounted audio":                   "steering_mounted_audio",
            "cooled glovebox":                          "cooled_glovebox",
            "rain sensing wipers":                      "rain_sensing_wipers",
            "automatic headlamps":                      "automatic_headlamps",
            "electric tailgate":                        "electric_tailgate",
            "adjustable orvms":                         "adjustable_orvms",
            "adjustable rear headrests":                "adjustable_rear_headrests",
            "power outlets":                            "power_outlets_12v",
            "12v power outlet":                         "power_outlets_12v",
            "wireless charger":                         "wireless_charger",
            "wireless phone charger":                   "wireless_phone_charger",
            "rear ac vent":                             "rear_ac_vent",
            "sunroof":                                  "electric_sunroof",
            "heads-up display":                         "head_up_display",
            "hud":                                      "head_up_display",
        },
    },
    # Locks & Security
    "locks_and_security": {
        "repeater_key": "field_6a2a4fed749fc",
        "spec_name_key": "field_6a2a4fed749fd",
        "spec_value_key": "field_6a2a4fed749fe",
        "choices": {
            "engine immobilizer":                       "engine_immobilizer",
            "central locking":                          "central_locking",
            "speed sensing door lock":                  "speed_sensing_door_lock",
            "speed sensing auto door lock":             "speed_sensing_door_lock",
            "child safety lock":                        "child_safety_lock",
        },
    },
    # Seats & Upholstery
    "seats_and_upholstery": {
        "repeater_key": "field_6a2a4fec749f9",
        "spec_name_key": "field_6a2a4fec749fa",
        "spec_value_key": "field_6a2a4fec749fb",
        "choices": {
            "driver seat adjustment":                   "driver_seat_adjustment",
            "front passenger seat":                     "front_passenger_seat_adjustment",
            "seat upholstery":                          "seat_upholstery",
            "upholstery":                               "seat_upholstery",
            "rear armrest":                             "rear_armrest",
            "front armrest":                            "front_armrest",
            "folding rear seat":                        "folding_rear_seat",
            "split rear seat":                          "split_rear_seat",
            "seatback pockets":                         "front_seatback_pockets",
            "front seatback pockets":                   "front_seatback_pockets",
            "headrests":                                "headrests",
            "ventilated seats":                         "ventilated_seats",
            "seat memory":                              "seat_memory_function",
            "isofix":                                   "isofix_child_seat_mounts",
            "isofix child seat":                        "isofix_child_seat_mounts",
        },
    },
    # Safety & Security
    "safety_and_security": {
        "repeater_key": "field_6a2a4fea749f6",
        "spec_name_key": "field_6a2a4fea749f7",
        "spec_value_key": "field_6a2a4fea749f8",
        "choices": {
            "no. of airbags":                           "no_of_airbags",
            "number of airbags":                        "no_of_airbags",
            "airbags":                                  "no_of_airbags",
            "driver airbag":                            "driver_airbags",
            "driver airbags":                           "driver_airbags",
            "passenger airbag":                         "passenger_airbags",
            "passenger airbags":                        "passenger_airbags",
            "side airbag":                              "side_airbags",
            "side airbags":                             "side_airbags",
            "rear side airbag":                         "rear_side_airbag",
            "curtain airbag":                           "curtain_airbag",
            "curtain airbags":                          "curtain_airbag",
            "knee airbag":                              "knee_airbags",
            "knee airbags":                             "knee_airbags",
            "abs":                                      "abs",
            "anti-lock braking system":                 "abs",
            "ebd":                                      "electronic_brakeforce_distribution",
            "electronic brakeforce distribution":       "electronic_brakeforce_distribution",
            "esp":                                      "electronic_stability_program",
            "electronic stability program":             "electronic_stability_program",
            "electronic stability control":             "electronic_stability_program",
            "day & night rear view mirror":             "day_night_rear_view_mirror",
            "day and night rear view mirror":           "day_night_rear_view_mirror",
            "hill hold control":                        "hill_hold_control",
            "hill assist":                              "hill_assist",
            "hill descent control":                     "hill_descent_control",
            "tyre pressure monitoring":                 "tyre_pressure_monitoring_system_tpms",
            "tpms":                                     "tyre_pressure_monitoring_system_tpms",
            "traction control":                         "traction_control",
            "blind spot monitoring":                    "blind_spot_monitoring",
            "bsm":                                      "blind_spot_monitoring",
            "lane departure warning":                   "lane_assist",
            "lane assist":                              "lane_assist",
            "forward collision warning":                "forward_collision_warning",
            "auto emergency braking":                   "auto_emergency_braking",
            "autonomous emergency braking":             "auto_emergency_braking",
            "driver drowsiness detection":              "driver_drowsiness_detection",
            "child seat anchor points":                 "child_seat_anchor_points",
            "parking sensors":                          "parking_sensors",
            "speed alert":                              "speed_alert_system",
            "speed alert system":                       "speed_alert_system",
            "parking assistance":                       "parking_assistance",
            "dashcam":                                  "dashcam",
            "electric orvm heating":                    "electric_orvm_heating",
            "360 degree camera":                        "360_view_camera",
            "360 view camera":                          "360_view_camera",
            "rear defogger":                            "rear_defogger",
        },
    },
    # Lights
    "lights": {
        "repeater_key": "field_6a2a4fe4749f3",
        "spec_name_key": "field_6a2a4fe4749f4",
        "spec_value_key": "field_6a2a4fe4749f5",
        "choices": {
            "light type":                               "light_type",
            "headlight type":                           "light_type",
            "drls":                                     "daytime_running_lamps_drls",
            "daytime running lamps":                    "daytime_running_lamps_drls",
            "daytime running lamps (drls)":             "daytime_running_lamps_drls",
            "turn indicators on orvm":                  "turn_indicators_on_orvm",
            "fog lamps":                                "fog_lamps",
            "ambient lighting":                         "ambient_lighting",
            "auto high beam":                           "automatic_high_beam_assist",
            "automatic high beam assist":               "automatic_high_beam_assist",
            "follow me home headlamps":                 "follow_me_home_headlamps",
            "follow-me-home headlamps":                 "follow_me_home_headlamps",
            "cornering lights":                         "cornering_lights",
            "cabin lamps":                              "cabin_lamps",
            "automatic headlamps":                      "automatic_headlamps",
        },
    },
    # Instrumentation
    "instrumentation": {
        "repeater_key": "field_6a2a4fe3749f0",
        "spec_name_key": "field_6a2a4fe3749f1",
        "spec_value_key": "field_6a2a4fe3749f2",
        "choices": {
            "instrument cluster":                       "instrument_cluster",
            "trip meter":                               "trip_meter",
            "average fuel efficiency":                  "average_fuel_efficiency_indicator",
            "average speed":                            "average_speed",
            "clock":                                    "clock",
            "low fuel warning":                         "low_fuel_warning",
            "door ajar warning":                        "door_ajar_warning",
            "adjustable cluster brightness":            "adjustable_cluster_brightness",
            "gear indicator":                           "gear_indicator",
            "shift indicator":                          "shift_indicator",
            "seat belt warning":                        "seat_belt_warning",
        },
    },
    # Entertainment
    "entertainment": {
        "repeater_key": "field_6a2a4ff6749ff",
        "spec_name_key": "field_6a2a4ff674a00",
        "spec_value_key": "field_6a2a4ff674a01",
        "choices": {
            "infotainment system":                      "infotainment_system",
            "touchscreen display":                      "touchscreen_display_size",
            "audio system":                             "audio_system",
            "tweeters":                                 "tweeters",
            "subwoofer":                                "subwoofer",
            "gps navigation":                           "gps_navigation",
            "navigation":                               "gps_navigation",
            "voice commands":                           "voice_commands",
            "smartphone connectivity":                  "smartphone_connectivity",
            "wireless display":                         "wireless_display_screen_mirroring",
            "screen mirroring":                         "wireless_display_screen_mirroring",
            "heads-up display":                         "heads_up_display_hud",
            "hud":                                      "heads_up_display_hud",
            "radio":                                    "radio",
            "touch screen":                             "touch_screen",
            "touch screen size":                        "touch_screen_size",
            "touchscreen size":                         "touch_screen_size",
            "bluetooth connectivity":                   "bluetooth_connectivity",
            "speakers front":                           "speakers_front",
            "speakers rear":                            "speakers_rear",
            "numbers of speakers":                      "numbers_of_speakers",
            "speakers":                                 "numbers_of_speakers",
            "apple carplay":                            "apple_carplay",
            "android auto":                             "android_auto",
            "fm/am/radio":                              "fm_am_radio",
            "usb & auxiliary input":                    "usb_and_auxiliary_input",
            "usb":                                      "usb_and_auxiliary_input",
            "aux":                                      "usb_and_auxiliary_input",
            "aux input":                                "usb_and_auxiliary_input",
            "portable charging cable":                  "portable_charging_cable",
            "wireless charger":                         "wireless_charger",
            "bluetooth":                                "bluetooth_connectivity",
        },
    },
    # Advanced Features
    "advanced_features": {
        "repeater_key": "field_6a2aa821472a1",
        "spec_name_key": "field_6a2aa821472a2",
        "spec_value_key": "field_6a2aa821472a3",
        "choices": {
            "connected car technology":                 "connected_car_technology",
            "ota updates":                              "over_the_air_updates_ota",
            "over-the-air updates":                     "over_the_air_updates_ota",
            "geofencing":                               "geofencing",
            "digital key":                              "digital_key",
            "ai voice assistance":                      "ai_voice_assistance",
            "auto park assist":                         "auto_park_assist",
            "autonomous driving level":                 "autonomous_driving_level",
            "sos button":                               "sos_button",
            # --- NEW (2026-08-12): needs matching WP choices added first
            # (see note in SPEC_TO_ENGINE_CHOICE above) — individual ADAS
            # ("Hyundai SmartSense" / equivalent) sub-features that the
            # Review accordion lists one-by-one instead of as a single
            # "autonomous driving level" value.
            "forward collision-avoidance assist":       "forward_collision_avoidance_assist",
            "fca":                                      "forward_collision_avoidance_assist",
            "forward collision avoidance assist":       "forward_collision_avoidance_assist",
            "lane following assist":                    "lane_following_assist",
            "lfa":                                       "lane_following_assist",
            "lane keeping assist":                       "lane_keeping_assist",
            "lka":                                       "lane_keeping_assist",
            "blind-spot collision avoidance":            "blind_spot_collision_avoidance",
            "bca":                                       "blind_spot_collision_avoidance",
            "blind-spot view monitor":                   "blind_spot_view_monitor",
            "bvm":                                        "blind_spot_view_monitor",
            "rear cross-traffic collision avoidance":    "rear_cross_traffic_collision_avoidance",
            "rcca":                                       "rear_cross_traffic_collision_avoidance",
            "driver attention warning":                  "driver_attention_warning",
            "daw":                                        "driver_attention_warning",
            "tacho meter":                               "tachometer",
            "tachometer":                                "tachometer",
            "electronic multi tripmeter":                "electronic_multi_tripmeter",
            "digital odometer":                          "digital_odometer",
            "leather seats":                             "leather_seats",
            "ventilated seat":                           "ventilated_seats",
            "leather steering wheel":                    "leather_wrapped_steering_wheel",
            "spare wheel":                               "spare_wheel",
            "no. of doors":                              "no_of_doors",
            "number of doors":                           "no_of_doors",
            "no. of free services":                      "no_of_service_free",
            "free services":                             "no_of_service_free",
            "service interval":                          "service_interval",
            "boot opening type":                         "boot_opening_type",
            "spare key":                                 "spare_key",
            "anti-theft alarm":                          "anti_theft_alarm",
            "anti theft alarm":                          "anti_theft_alarm",
            "emission norm":                             "emission_norm",
            "emission standard":                         "emission_norm",
            "gcc specs":                                 "gcc_specs",
            "country of origin":                         "country_of_origin",
            "roadside assistance":                       "roadside_assistance",
            "extended warranty available":               "extended_warranty_available",
            "extended warranty":                         "extended_warranty_available",
            "service cost estimate":                     "service_cost_estimate",
        },
    },
    # Manufacturer Warranty
    "manufacturer_warranty": {
        "repeater_key": "field_6a2aa826472a4",
        "spec_name_key": "field_6a2aa826472a5",
        "spec_value_key": "field_6a2aa826472a6",
        "choices": {
            "battery warranty":                         "type_battery_warranty",
            "type battery warranty":                    "type_battery_warranty",
            "manufacturer warranty":                    "type_manufacturer_warranty",
            "type manufacturer warranty":               "type_manufacturer_warranty",
            "warranty":                                 "type_manufacturer_warranty",
        },
    },
    # NEW (2026-08-13): Exterior — zigwheels.ae's real /specifications page uses
    # "Exterior" as its own top-level category (separate from "Lights"). Field keys
    # below match acf-import-features-update.json — import that file in WP Admin
    # (ACF -> Tools -> Import) BEFORE this group will actually save anything;
    # until then, rows routed here are valid JSON but the ACF REST API will
    # reject them (field doesn't exist in WP yet).
    "exterior_features": {
        "repeater_key":   "field_6afe10000e01",
        "spec_name_key":  "field_6afe10000e011",
        "spec_value_key": "field_6afe10000e012",
        "choices": {
            "wheel covers":                               "wheel_covers",
            "roof rails":                                 "roof_rails",
            "rear spoiler":                                "rear_spoiler",
            "spoiler":                                     "rear_spoiler",
            "body colored bumpers":                        "body_colored_bumpers",
            "body coloured bumpers":                       "body_colored_bumpers",
            "body colored door handles":                   "body_colored_door_handles",
            "body colored orvm":                           "body_colored_orvm",
            "body coloured orvm":                          "body_colored_orvm",
            "chrome grille":                               "chrome_grille",
            "chrome garnish":                              "chrome_garnish",
            "shark fin antenna":                           "shark_fin_antenna",
            "power antenna":                               "power_antenna",
            "rear windshield wiper":                       "rear_windshield_wiper",
            "rear wiper":                                  "rear_windshield_wiper",
            "rear defogger":                               "rear_window_defogger",
            "tail lamps":                                  "tail_lamp_type",
            "orvm turn indicators":                        "orvm_turn_indicators",
            "skid plates":                                 "skid_plates",
            "sunroof type":                                "sunroof_type",
            "sunroof":                                     "sunroof_type",
            "body kit":                                    "body_kit",
            "available colors":                            "available_colours",
            "available colours":                           "available_colours",
            "adjustable headlights":                     "adjustable_headlights",
            "power adjustable exterior rear view mirror": "power_adjustable_exterior_rear_view_mirror",
            "tinted glass":                              "tinted_glass",
            "headlamp type":                             "headlamp_type",
            "rear window defogger":                      "rear_window_defogger",
            "alloy wheels":                              "alloy_wheels",
            "outside rear view mirror turn indicator":   "orvm_turn_indicators",
            "tail lamp type":                            "tail_lamp_type",
        },
    },
    # NEW (2026-08-13): Interior — general interior spec items that aren't a
    # seat/upholstery sub-field and aren't an instrument-cluster item. Same
    # import-first caveat as exterior_features above.
    "interior_features": {
        "repeater_key":   "field_6afe10000e02",
        "spec_name_key":  "field_6afe10000e021",
        "spec_value_key": "field_6afe10000e022",
        "choices": {
            "upholstery material":                         "upholstery_material",
            "interior colour theme":                       "interior_colour_theme",
            "interior color theme":                        "interior_colour_theme",
            "dashboard trim":                              "dashboard_trim",
            "leather wrapped steering wheel":              "leather_wrapped_steering_wheel",
            "leather steering wheel":                      "leather_wrapped_steering_wheel",
            "leather wrapped gear knob":                   "leather_wrapped_gear_knob",
            "leather gear knob":                           "leather_wrapped_gear_knob",
            "rear seat recline":                           "rear_seat_recline",
            "sun visor with vanity mirror":                "sun_visor_with_vanity_mirror",
            "vanity mirror":                               "sun_visor_with_vanity_mirror",
            "rear reading lamp":                           "rear_reading_lamp",
            "cooled cup holders":                          "cooled_cup_holders",
            "grab handles":                                "grab_handles",
            "floor console":                               "floor_console",
            "ambient interior lighting":                   "ambient_interior_lighting",
            "ambient lighting":                            "ambient_interior_lighting",
            "rear center armrest":                         "rear_center_armrest",
            "rear centre armrest":                         "rear_center_armrest",
            "boot lid garnish":                            "boot_lid_garnish",
        },
    },
}

# Specifications repeater field keys per category
SPEC_REPEATERS = {
    "engine_and_performance": {
        "repeater_key":   "field_6a2a3bcd9b778",
        "spec_name_key":  "field_6a2a4d9410d13",
        "spec_value_key": "field_6a2a4d9f10d14",
        "choices":        SPEC_TO_ENGINE_CHOICE,
    },
    "dimensions_and_weight": {
        "repeater_key":   "field_6a2a4e60ae7e7",
        "spec_name_key":  "field_6a2a4e60ae7e8",
        "spec_value_key": "field_6a2a4e60ae7e9",
        "choices":        SPEC_TO_DIMENSIONS_CHOICE,
    },
    "capacity": {
        "repeater_key":   "field_6a2a4e63ae7ea",
        "spec_name_key":  "field_6a2a4e63ae7eb",
        "spec_value_key": "field_6a2a4e63ae7ec",
        "choices":        SPEC_TO_CAPACITY_CHOICE,
    },
    "fuel_efficiency": {
        "repeater_key":   "field_6a2a4e64ae7ed",
        "spec_name_key":  "field_6a2a4e64ae7ee",
        "spec_value_key": "field_6a2a4e64ae7ef",
        "choices":        SPEC_TO_FUEL_CHOICE,
    },
    "suspension_brakes_and_steering": {
        "repeater_key":   "field_6a2a4e6dae7f0",
        "spec_name_key":  "field_6a2a4e6dae7f1",
        "spec_value_key": "field_6a2a4e6dae7f2",
        "choices":        SPEC_TO_SUSPENSION_CHOICE,
    },
    "tyres_and_wheels": {
        "repeater_key":   "field_6a2a4e71ae7f3",
        "spec_name_key":  "field_6a2a4e71ae7f4",
        "spec_value_key": "field_6a2a4e71ae7f5",
        "choices":        SPEC_TO_TYRES_CHOICE,
    },
    "battery_range_and_charging": {
        "repeater_key":   "field_6a2bf0f2292c4",
        "spec_name_key":  "field_6a2bf0f2292c5",
        "spec_value_key": "field_6a2bf0f2292c6",
        "choices":        SPEC_TO_BATTERY_CHOICE,
    },
}

# zigwheels.ae /specifications category headings -> ACF repeater field name.
# zigwheels.ae's own category wording isn't confirmed pixel-for-pixel (the page
# is rendered per-model), so this list is intentionally generous with synonyms.
# If a category comes through un-mapped, _try_match() still falls back to
# matching every repeater's spec-name dictionary directly, so nothing is lost —
# add the exact heading you see in scraped data here for cleaner routing.
CATEGORY_TO_GROUP: dict[str, str] = {
    "engine & performance":              "engine_and_performance",
    "engine and performance":            "engine_and_performance",
    "engine & transmission":             "engine_and_performance",
    "engine and transmission":           "engine_and_performance",
    "engine specifications":             "engine_and_performance",
    "performance":                       "engine_and_performance",
    "transmission":                      "engine_and_performance",
    "dimensions & weight":               "dimensions_and_weight",
    "dimensions and weight":             "dimensions_and_weight",
    "dimensions":                        "dimensions_and_weight",
    # zigwheels.ae's real /specifications page groups Dimensions and Capacity
    # (boot space, fuel tank, seating) under ONE heading — hint to
    # dimensions_and_weight first; _try_match()'s full-group fallback scan
    # (see _build_spec_repeaters) still finds a capacity-only spec name
    # (e.g. "Boot Space") even when the hint points elsewhere.
    "dimensions & capacity":             "dimensions_and_weight",
    "dimensions and capacity":           "dimensions_and_weight",
    "capacity":                          "capacity",
    "fuel efficiency":                   "fuel_efficiency",
    "fuel economy":                      "fuel_efficiency",
    "fuel consumption":                  "fuel_efficiency",
    "mileage":                           "fuel_efficiency",
    "suspension brakes & steering":      "suspension_brakes_and_steering",
    "suspension, brakes & steering":     "suspension_brakes_and_steering",
    "suspension, brakes and steering":   "suspension_brakes_and_steering",
    "steering, suspension & brakes":     "suspension_brakes_and_steering",
    "chassis and suspension":            "suspension_brakes_and_steering",
    # zigwheels.ae's real /specifications page also lists these as their OWN
    # separate tabs ("Suspension", "Steering", "Brakes & Tyres") rather than
    # one combined heading — all three still route to the same ACF repeater
    # (it already has front/rear suspension, brake type, AND steering type
    # sub-choices); "brakes & tyres" additionally falls through to
    # tyres_and_wheels via the full-group scan for the tyre-specific rows.
    "suspension":                        "suspension_brakes_and_steering",
    "steering":                          "suspension_brakes_and_steering",
    "brakes & tyres":                    "suspension_brakes_and_steering",
    "brakes and tyres":                  "suspension_brakes_and_steering",
    "brakes":                            "suspension_brakes_and_steering",
    "tyres & wheels":                    "tyres_and_wheels",
    "tyres and wheels":                  "tyres_and_wheels",
    "wheels and tyres":                  "tyres_and_wheels",
    "battery, range & charging":         "battery_range_and_charging",
    "battery range and charging":        "battery_range_and_charging",
    "battery and charging":              "battery_range_and_charging",
    "comfort & convenience":             "comfort_and_convenience",
    "comfort and convenience":           "comfort_and_convenience",
    "comfort features":                  "comfort_and_convenience",
    "locks & security":                  "locks_and_security",
    "locks and security":                "locks_and_security",
    "seats & upholstery":                "seats_and_upholstery",
    "seats and upholstery":              "seats_and_upholstery",
    "safety & security":                 "safety_and_security",
    "safety and security":               "safety_and_security",
    "safety features":                   "safety_and_security",
    "safety":                            "safety_and_security",
    "lights":                            "lights",
    "instrumentation":                   "instrumentation",
    "entertainment":                     "entertainment",
    "entertainment & communication":     "entertainment",
    "entertainment and communication":   "entertainment",
    "infotainment":                      "entertainment",
    "infotainment & connectivity":       "entertainment",
    "connectivity":                      "entertainment",
    "advanced features":                 "advanced_features",
    "adas":                              "advanced_features",
    "driver assistance":                 "advanced_features",
    "driver assistance systems":         "advanced_features",
    "manufacturer warranty":             "manufacturer_warranty",
    "warranty":                          "manufacturer_warranty",
    # NEW (2026-08-13) — real zigwheels.ae top-level tabs that had NO group
    # to route into before (see acf-import-features-update.json for the new
    # WP fields these need):
    "exterior":                          "exterior_features",
    "exterior features":                 "exterior_features",
    "interior":                          "interior_features",
    "interior features":                 "interior_features",
    "miscellaneous":                     "advanced_features",
    "misc":                              "advanced_features",
}

ALL_REPEATERS: dict = {**SPEC_REPEATERS, **FEATURE_REPEATERS}

# Cars Overview (cars-data model post) field keys
# price_range_aed / gcc_specs added 2026-08-11 — pulled from acf-export-2026-08-11.json,
# these existed in WP but were never added here, so the mapper silently never sent them.
MODEL_OVERVIEW_FIELDS = {
    "cars_gallery":    "field_6a2f7d87e8034",
    "launched_date":   "field_6a295294cc3d4",
    "price_range":     "field_6a295419cc3d5",
    "body_type":       "field_6a29543ccc3d6",
    "fuel_type":       "field_6a29544bcc3d7",
    "transmission":    "field_6a29545bcc3d8",
    "mileage":         "field_6a295465cc3d9",
    "price_range_aed": "field_6a79a14c6c0b2",
    "gcc_specs":       "field_6a79a1566c0b3",
}

# Key Specifications (cars-data model post) field keys
# top_speed / fuel_consumption added 2026-08-11 — same as above, existed in WP,
# missing here.
KEY_SPEC_FIELDS = {
    "engine_cc":          "field_6a295532bb528",
    "max_torque":         "field_6a295552bb529",
    "no_of_airbags":      "field_6a295561bb52a",
    "bootspace":          "field_6a295566bb52b",
    "ground_clearance":   "field_6a295571bb52c",
    "fuel_tank_capacity": "field_6a29557bbb52d",
    "seating_capacity":   "field_6a29558bbb52e",
    "drive_type":         "field_6a295594bb52f",
    "top_speed":          "field_6a79a17b87e51",
    "fuel_consumption":   "field_6a79a18987e52",
}

# Latest Updates repeater (cars-data) field keys
LATEST_UPDATES_REPEATER = "field_6a2955fa1e584"
LATEST_UPDATES_HEADLINE  = "field_6a2956691e585"
LATEST_UPDATES_DATE      = "field_6a2956911e586"
# WP field group caps this repeater at 5 rows (see _build_latest_updates_acf) —
# sending more trips a rest_too_many_items HTTP 400 on CREATE.
LATEST_UPDATES_MAX_ROWS = 5

# What's New Content (cars-data) field keys
WHATS_NEW_FIELDS = {
    "introduction_list":  "field_6a2957676bf68",
    "introduction_item":  "field_6a2957a06bf69",
    "introduction_image": "field_6a2958436bf6a",
    "exterior_list":      "field_6a2958996bf6b",
    "exterior_item":      "field_6a2958a96bf6c",
    "exterior_image":     "field_6a2a3903b645f",
    "interior_list":      "field_6a2a392ab6460",
    "interior_item":      "field_6a2a393eb6461",
    "interior_image":     "field_6a2a394fb6462",
    "powertrain_list":    "field_6a2a3978b6463",
    "powertrain_item":    "field_6a2a3985b6464",
    "powertrain_image":   "field_6a2a3993b6465",
}

# Parent Base Model (car-variant post) — links variant to its cars-data parent
PARENT_BASE_MODEL_FIELD = "parent_base_model"

WHATS_NEW_REPEATER_MAP = {
    "introduction": ("introduction_list", "introduction_item"),
    "exterior":     ("exterior_list",     "exterior_item"),
    "interior":     ("interior_list",     "interior_item"),
    "powertrain":   ("powertrain_list",   "powertrain_item"),
}


def _normalise_key(name: str) -> str:
    """Lowercase + collapse whitespace for map lookup."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _normalise_category(category: str) -> str:
    """Normalise scraper category labels for CATEGORY_TO_GROUP lookup.

    FIX (2026-08-12): the Review-accordion sub-headings that
    _extract_categorized_specs() uses as categories are free-form review
    prose, not the tidy labels this dict was written against — e.g. "Core
    Dimensions:", "Active Safety (Standard Across All Trims)", "Climate
    Control: The Critical Differentiator". We now strip trailing colons and
    parenthetical asides so more of them line up with CATEGORY_TO_GROUP (and
    _match_category_group() below adds substring-based fuzzy matching on
    top for the rest).
    """
    cat = str(category or "").strip().lower()
    cat = cat.replace("&", " and ")
    cat = re.sub(r"\s*\([^)]*\)", "", cat)     # "(Standard Across All Trims)"
    cat = cat.split(":", 1)[0]                 # "Core Dimensions:" -> "core dimensions"
    return re.sub(r"\s+", " ", cat).strip()


def _match_category_group(cat: str) -> str | None:
    """Resolve a normalised scraped category to a SPEC_REPEATERS/
    FEATURE_REPEATERS group name. Tries an exact CATEGORY_TO_GROUP hit
    first, then falls back to substring containment either direction (e.g.
    "core dimensions" ~ "dimensions", "chassis dynamics and ride comfort" ~
    "suspension brakes and steering" via shared words) so review-prose
    sub-headings that don't exactly match the curated label list still
    route to the right repeater instead of relying purely on the spec-name
    fallback scan.
    """
    if not cat:
        return None
    if cat in CATEGORY_TO_GROUP:
        return CATEGORY_TO_GROUP[cat]
    for label, group in CATEGORY_TO_GROUP.items():
        if label in cat or cat in label:
            return group
    return None


def _lookup_choice(norm: str, choices: dict[str, str], fuzzy: bool = True) -> str | None:
    """
    Match a scraped spec name to an ACF select choice key.

    fuzzy=False restricts matching to exact/near-exact forms only (used for
    SPEC_REPEATERS — see Bug 9 note in _build_spec_repeaters). Measured-value
    spec names need to be that precise: a false-positive match there
    overwrites a real number/figure with the wrong value. fuzzy=True (used
    for FEATURE_REPEATERS) allows substring/word matching, which is safe
    there because features only ever carry "Yes"/"No", so a false positive
    is at worst a spurious duplicate flag, not corrupted numeric data.
    """
    if norm in choices:
        return choices[norm]

    variants = [
        re.sub(r"\s*\([^)]*\)", "", norm).strip(),
        norm.replace("-", " "),
        re.sub(r"\s*\([^)]*\)", "", norm.replace("-", " ")).strip(),
    ]
    for candidate in variants:
        if candidate and candidate in choices:
            return choices[candidate]

    if not fuzzy:
        return None

    # FUZZY FALLBACK (2026-08-12): scraped spec names now come from free-form
    # review prose ("Front Headroom", "Wheelbase", "Ground Clearance
    # (Unladen)", "Approach Angle") rather than the site's old tabular
    # /specifications table this choices dict was written against, so exact
    # matches above miss most real spec names — this was the main cause of
    # Specifications ACF repeaters coming through almost entirely empty
    # while Features (simpler yes/no names) still worked. We try, in order
    # of confidence:
    #   1. the choice label is fully contained in the spec name (or the
    #      spec name is fully contained in the choice label)
    #   2. every "significant" word (len >= 4) in the choice label appears
    #      somewhere in the spec name
    base = re.sub(r"\s*\([^)]*\)", "", norm).strip()

    def _contains_whole(haystack: str, needle: str) -> bool:
        # FIX (2026-08-13) — Bug 9 continued: plain `needle in haystack`
        # matched "power" inside "power steering" and "steering" inside
        # "multi-function steering wheel", corrupting unrelated repeaters.
        # Require the shorter string to appear as whole word(s) at a word
        # boundary, not as a raw substring of a longer, unrelated phrase.
        if not needle:
            return False
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None

    best_key, best_score = None, -1
    for choice_label, choice_key in choices.items():
        cl = choice_label.strip()
        if not cl:
            continue
        if cl == base:
            return choice_key
        if _contains_whole(base, cl) or _contains_whole(cl, base):
            # Prefer the choice label whose length is CLOSEST to the spec
            # name's — e.g. for spec name "engine", prefer "engine type"
            # (close match) over "engine displacement" (also contains
            # "engine" but is a much longer, less-related label).
            score = 1000 - abs(len(cl) - len(base))
        else:
            words = [w for w in re.split(r"[\s/]+", cl) if len(w) >= 4]
            score = (
                sum(len(w) for w in words)
                if words and all(_contains_whole(base, w) for w in words)
                else -1
            )
        if score > best_score:
            best_score, best_key = score, choice_key
    return best_key if best_score >= 0 else None


def _to_wp_slug(text: str) -> str:
    """Convert 'Hyundai Creta 1.5L Premium' -> 'hyundai-creta-1-5l-premium'."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _yes_no(value: str) -> str:
    """Normalise boolean-ish spec values to 'Yes' / 'No' / original."""
    low = str(value).strip().lower()
    if low in ("yes", "available", "standard", "true", "1"):
        return "Yes"
    if low in ("no", "not available", "na", "n/a", "false", "0", "-"):
        return "No"
    return value.strip()


def _first_number(text: str) -> str:
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text or ""))
    return m.group(0).replace(",", "") if m else ""


def _extract_fuel_consumption(model: dict, specs: list) -> str:
    """
    Resolve Cars Overview 'mileage' field for the model post.

    zigwheels.ae shows this as a UAE-style combined fuel-consumption figure
    (or an EV range) rather than India's ARAI test-cycle number. Checked in
    priority order: the quick 'Key Specs' box, then the categorized
    /specifications table, then variant-level spec summaries.
    """
    key_specs = model.get("key_specs") or {}
    for label in ("Mileage", "Fuel Consumption", "Range", "Combined Fuel Consumption"):
        if key_specs.get(label):
            return str(key_specs[label]).strip()

    fuel_names = {
        "fuel consumption", "combined fuel consumption", "average fuel consumption",
        "mileage", "range", "wltp range", "electric range", "fuel efficiency",
    }
    for item in specs or []:
        if not isinstance(item, dict):
            continue
        if _normalise_key(item.get("name", "")) in fuel_names and item.get("value"):
            return str(item["value"]).strip()

    return ""


def _airbag_row_weight(norm_name: str) -> int:
    """How many physical airbags one Yes row represents."""
    if "curtain" in norm_name:
        return 2
    if "side" in norm_name and "airbag" in norm_name:
        return 2
    return 1


def _extract_airbag_count(key_specs: dict, specs: list) -> str:
    """
    Resolve Key Specifications no_of_airbags.

    The zigwheels.ae 'Key Specs' box lists airbag types as a comma-separated
    string (e.g. 'Driver Airbag, Passenger Airbag') rather than a total count —
    count the entries when no explicit total is present.
    """
    airbag_text = str(key_specs.get("Airbag", "")).strip()
    if airbag_text:
        num = re.search(r"^\d+$", airbag_text)
        if num:
            return num.group(0)
        parts = [p for p in re.split(r",|/|&|\band\b", airbag_text, flags=re.I) if p.strip()]
        if parts:
            return str(len(parts))

    for item in specs or []:
        if not isinstance(item, dict):
            continue
        norm = _normalise_key(item.get("name", ""))
        if norm in {"no. of airbags", "number of airbags", "airbags", "total airbags"}:
            val = str(item.get("value", "")).strip()
            if val:
                num = re.search(r"\d+", val)
                return num.group(0) if num else val

    total = 0
    for item in specs or []:
        if not isinstance(item, dict):
            continue
        norm = _normalise_key(item.get("name", ""))
        if "airbag" not in norm:
            continue
        val = str(item.get("value", "")).strip()
        low = val.lower()
        if low in ("no", "not available", "na", "n/a", "false", "0", "-"):
            continue
        num = re.match(r"^\d+$", val)
        if num:
            total += int(val)
        elif low in ("yes", "available", "standard", "true", "1"):
            total += _airbag_row_weight(norm)

    return str(total) if total > 0 else ""


def _is_junk_spec_item(name: str, value: str, category: str) -> bool:
    """
    Filter out rows that aren't real spec/feature data at all.

    FIX (2026-08-13): _extract_categorized_specs() on scrapper.py's side
    sometimes captures the Review accordion's "Other <Segment> Cars to
    Consider Instead of the <Model>" comparison table (a rivals table, not
    a spec table) as if it were a normal category. Its "values" are numbered
    reason-lists ("1. More powerful engine 2. More engaging driving …"),
    never real spec figures, so they're easy to recognise and drop instead
    of being fuzzy-matched into an unrelated ACF choice.
    """
    val = str(value or "").strip()
    if re.match(r"^\d+[.)]\s", val):
        return True
    if re.search(r"\bconsider\b.*\binstead of\b", str(category or ""), re.I):
        return True
    return False


def _strip_colour_noise(specs: list, colours: list) -> list:
    """
    FIX (2026-08-13): zigwheels.ae's Review accordion sometimes lists paint
    colour swatches ("Phantom Black", "Ceramic White", ...) as bullet items
    under an "Exterior" heading, formatted exactly like a real boolean
    feature ("Phantom Black": "Yes"). These aren't specs/features at all —
    they duplicate the separately-scraped model["colours"] list — and since
    they never match any real ACF choice they were previously just silently
    dropped, which was harmless but wasteful. Filter them out explicitly so
    they don't get wrongly fuzzy-matched into an unrelated Exterior spec
    choice now that a real "exterior_features" repeater exists to match
    against (see CATEGORY_TO_GROUP).
    """
    if not colours:
        return specs
    colour_names = {str(c).strip().lower() for c in colours if c}
    out = []
    for item in specs or []:
        if not isinstance(item, dict):
            continue
        cat = _normalise_category(item.get("category", ""))
        name = _normalise_key(item.get("name", ""))
        if cat == "exterior" and name in colour_names:
            continue
        out.append(item)
    return out


def _build_spec_repeaters(specs: list, features: list) -> dict:
    """
    Walk specs and features and map each recognised item into the correct
    ACF repeater group. Returns {repeater_group_name: [row, ...], …}

    Each row uses ACF sub-field names ("spec_name", "spec_value") so the REST
    API can map values into repeater rows.

    zigwheels.ae's /specifications page produces items like:
      {"category": "Dimensions & Weight", "name": "Ground Clearance", "value": "190 mm"}
    We try to match by spec name; category is used only as a tiebreak.

    FIX (2026-08-13) — Bug 9: specs and features used to be concatenated
    into one list and matched against EVERY repeater (spec AND feature
    groups) indiscriminately. Because _lookup_choice()'s fuzzy fallback does
    substring containment, plain boolean features like "Power Steering" or
    "Multi-function Steering Wheel" were matching the *measured-value*
    engine/suspension choices "power" / "steering_type" — overwriting real
    numbers (actual engine power, actual steering type) with the literal
    string "Yes". This was the direct cause of the live site showing
    "Power: Yes" / "Top Speed: Yes" / "Height: Yes" etc. instead of real
    figures. Specs (real measured values) are now only matched against
    SPEC_REPEATERS; features (Yes/No flags) are only matched against
    FEATURE_REPEATERS — the two never cross.
    """
    repeater_data: dict[str, list] = {}

    def _add_row(group_name: str, choice_key: str, value: str):
        if group_name not in repeater_data:
            repeater_data[group_name] = []
        existing_choices = {r.get("spec_name") for r in repeater_data[group_name]}
        if choice_key not in existing_choices:
            preserve = group_name in {
                "fuel_efficiency", "manufacturer_warranty", "lights",
                "engine_and_performance", "dimensions_and_weight", "capacity",
                "tyres_and_wheels", "instrumentation", "entertainment",
                "battery_range_and_charging",
            }
            spec_value = str(value).strip() if preserve else _yes_no(value)
            repeater_data[group_name].append({
                "spec_name":  choice_key,
                "spec_value": spec_value,
            })

    def _try_match(group_name: str, norm: str, raw_val: str) -> bool:
        rconf = ALL_REPEATERS.get(group_name)
        if not rconf:
            return False
        # Only allow loose/fuzzy matching for FEATURE_REPEATERS (safe: worst
        # case is a spurious "Yes"). SPEC_REPEATERS require an exact/
        # near-exact spec-name match (see _lookup_choice docstring).
        is_feature_group = group_name in FEATURE_REPEATERS
        choice_key = _lookup_choice(norm, rconf["choices"], fuzzy=is_feature_group)
        if choice_key:
            _add_row(group_name, choice_key, raw_val)
            if group_name == "tyres_and_wheels" and norm in TYRE_SIZE_MIRROR:
                _add_row(group_name, TYRE_SIZE_MIRROR[norm], raw_val)
            return True
        return False

    def _walk(items: list, allowed_groups: dict):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name", "")
            raw_val  = item.get("value", "")
            cat_raw  = item.get("category", "")
            if not raw_name or not raw_val:
                continue
            if _is_junk_spec_item(raw_name, raw_val, cat_raw):
                continue
            norm = _normalise_key(raw_name)
            cat  = _normalise_category(cat_raw)

            group_hint = _match_category_group(cat)
            if group_hint and group_hint in allowed_groups and _try_match(group_hint, norm, raw_val):
                continue

            for group_name in allowed_groups:
                if _try_match(group_name, norm, raw_val):
                    break

    # `features` (from the "Compare Variants" table) is a PURE boolean
    # Yes/No flag list — it never carries real measured values, so it only
    # ever searches FEATURE_REPEATERS. This is what fixes Bug 9.
    #
    # `specs` (from the Review-accordion categorized specs) is legitimately
    # a MIXED bag — the same list holds real measured values ("Length":
    # "4,710 mm") under Dimensions/Engine categories AND boolean Yes/No
    # feature-style items ("ABS with EBD": "Yes", "Blind Spot Warning...":
    # "Yes") under Safety/Interior categories — so it still searches
    # ALL_REPEATERS (both spec and feature groups). The tightened
    # word-boundary fuzzy matching above is what keeps this safe now
    # instead of the old raw-substring matching that caused Bug 9.
    _walk(specs, ALL_REPEATERS)
    _walk(features, FEATURE_REPEATERS)

    return repeater_data


def _extract_key_specs(model: dict, specs: list) -> dict:
    """
    Extract Key Specifications fields for the cars-data model post.
    Sources, in priority order: the zigwheels.ae 'Key Specs' quick box
    (model["key_specs"]), then the categorized /specifications table.
    Returns {field_name: value}.
    """
    key_specs = model.get("key_specs") or {}
    result: dict[str, str] = {}

    if key_specs.get("Engine"):
        result["engine_cc"] = _first_number(key_specs["Engine"]) or key_specs["Engine"]
    if key_specs.get("Torque"):
        result["max_torque"] = key_specs["Torque"]
    if key_specs.get("Drive Type"):
        result["drive_type"] = key_specs["Drive Type"]
    if key_specs.get("Seating"):
        result["seating_capacity"] = _first_number(key_specs["Seating"]) or key_specs["Seating"]

    # FIX (2026-08-17): this used to require an EXACT string match against a
    # tiny fixed set ("ground clearance", "seating capacity", ...) with no
    # tolerance for the parenthetical suffixes zigwheels' real spec names
    # commonly carry — e.g. "Ground Clearance (Unladen)" or "Seating
    # Capacity (Persons)" never matched "ground clearance"/"seating
    # capacity", so those Key Specification fields came through empty even
    # though the scraper had the data right there in `specs`. Reuse
    # _lookup_choice's paren-stripping/dash-normalising logic (fuzzy=True is
    # safe here — these are plain text fields, not WP select choices, so a
    # slightly-loose match just fills in a real value instead of leaving the
    # field blank; there's no "invalid choice" 400 risk like SPEC_REPEATERS).
    spec_map = {
        "engine_cc":          {"engine displacement", "displacement", "displacement (cc)"},
        "max_torque":         {"max torque", "torque"},
        "bootspace":          {"boot space", "cargo volume", "luggage capacity", "cargo capacity"},
        "ground_clearance":   {"ground clearance", "ground clearance (unladen)", "ground clearance (laden)"},
        "fuel_tank_capacity": {"fuel tank capacity", "fuel tank"},
        "seating_capacity":   {"seating capacity", "seating capacity (persons)", "no. of seats", "number of seats"},
        "drive_type":         {"drive type", "drive train", "drivetrain"},
        "top_speed":          {"top speed"},
    }
    for spec_field, names in spec_map.items():
        if result.get(spec_field):
            continue
        choices = {name: spec_field for name in names}
        for item in specs or []:
            if not isinstance(item, dict):
                continue
            raw_name, raw_val = item.get("name", ""), item.get("value", "")
            if not raw_name or not raw_val:
                continue
            if _lookup_choice(_normalise_key(raw_name), choices, fuzzy=True):
                result[spec_field] = raw_val
                break

    airbag_count = _extract_airbag_count(key_specs, specs)
    if airbag_count:
        result["no_of_airbags"] = airbag_count

    # fuel_consumption: added in WP 2026-08-11 (see KEY_SPEC_FIELDS). Same
    # source figure as Cars Overview's "mileage" field (map_model_to_acf()
    # sets that one separately via _extract_fuel_consumption()) — this just
    # mirrors it into the new dedicated Key Specs field.
    fuel_consumption = _extract_fuel_consumption(model, specs)
    if fuel_consumption:
        result["fuel_consumption"] = fuel_consumption

    return result


def _extract_overview(model: dict) -> dict:
    """Build Cars Overview ACF fields for the cars-data post (by field name)."""
    key_specs = model.get("key_specs") or {}
    price_display = model.get("price_display", "")
    transmission = key_specs.get("Transmission", "")
    if not transmission:
        # FIX (2026-08-17): the quick "Key Specs" box doesn't always carry a
        # Transmission row — fall back to the categorized spec list (e.g.
        # "Transmission Type"/"Gearbox Type": "6-Speed Automatic") so this
        # doesn't come through empty just because that one summary box
        # skipped it.
        for item in model.get("specifications") or []:
            if not isinstance(item, dict):
                continue
            name = _normalise_key(item.get("name", ""))
            if name in {"transmission", "transmission type", "gearbox", "gearbox type", "gear box"} and item.get("value"):
                transmission = item["value"]
                break
    return {
        "launched_date":  model.get("model_year", ""),
        "price_range":    price_display,
        "body_type":      model.get("body_type", ""),
        "fuel_type":      key_specs.get("Fuel Type", ""),
        "transmission":   transmission,
        "mileage":        "",   # filled from specs below
        # price_range_aed / gcc_specs: added in WP 2026-08-11 (see MODEL_OVERVIEW_FIELDS).
        # zigwheels.ae prices are already AED-denominated, so this mirrors price_range
        # rather than re-deriving it — keep both fields in sync if that ever changes.
        "price_range_aed": price_display,
        "gcc_specs":        _extract_gcc_specs(model),
    }


def _extract_gcc_specs(model: dict) -> str:
    """
    'GCC Specs' vs 'Other Specs' only shows up on zigwheels.ae when the page
    explicitly calls it out (FAQ text, intro paragraph) — it's not a
    guaranteed field, so this only returns Yes/No when the source text
    actually says so, rather than assuming every UAE listing is GCC spec.
    """
    haystacks = [model.get("price_overview_text", ""), model.get("spec_overview_text", "")]
    haystacks += [f.get("answer", "") for f in (model.get("faqs") or [])]
    text = " ".join(h for h in haystacks if h)
    if re.search(r"\bGCC\s*Specs?\b", text, re.I):
        if re.search(r"\bnon[- ]GCC\b|\bnot\s+GCC\b", text, re.I):
            return "No"
        return "Yes"
    return ""


def _build_whats_new_acf(model: dict) -> dict:
    """
    Map the "<Model> Review" accordion's narrative paragraphs -> What's New
    ACF repeater fields on cars-data (introduction/exterior/interior/
    powertrain_list).

    FIX (2026-08-12): this used to dump the "<Model> <Year> Updates" dated
    timeline into introduction_list, which is wrong on two counts — (1) that
    timeline belongs in the separate *Latest Updates* ACF group (now handled
    by _build_latest_updates_acf below), and (2) it left exterior_list/
    interior_list/powertrain_list permanently empty since nothing was ever
    written to them. The source for What's New is now
    model["review_narrative"] (built by scrapper._extract_review_narrative
    from the Review accordion's Summary/Interior/Exterior/Engine & Fuel
    Consumption tabs), routed through WHATS_NEW_REPEATER_MAP.
    """
    narrative = model.get("review_narrative") or {}
    acf: dict = {}
    for src_key, (list_field, item_field) in WHATS_NEW_REPEATER_MAP.items():
        texts = narrative.get(src_key) or []
        rows = [{item_field: t} for t in texts if t]
        if rows:
            acf[list_field] = rows
    return acf


def _build_latest_updates_acf(model: dict) -> dict:
    """
    Map the zigwheels.ae "<Model> <Year> Updates" dated timeline -> the
    *Latest Updates Content* ACF group's latest_updates_list repeater
    (sub-fields: headline, update_date).

    This group's field keys (LATEST_UPDATES_REPEATER/HEADLINE/DATE) were
    defined but never actually wired into a payload before, which is why
    Latest Updates always came through empty — this was the fix.

    FIX (2026-08-17): the WP field group's "latest_updates_list" repeater
    has a max-rows limit of 5 (rest_too_many_items / HTTP 400 on CREATE if
    you send more) — models with a long update history were getting
    rejected outright. zigwheels.ae lists updates newest-first, so this
    keeps only the 5 most recent rather than failing the whole import.
    """
    updates = model.get("updates") or []
    rows = [
        {"headline": u.get("text", ""), "update_date": u.get("date", "")}
        for u in updates if u.get("text")
    ]
    rows = rows[:LATEST_UPDATES_MAX_ROWS]
    return {"latest_updates_list": rows} if rows else {}


def _keyed_acf_fields(field_name_map: dict, values: dict) -> dict:
    """Convert {field_name: value} -> {field_key: value} so wp_uploader can
    POST directly into the ACF REST API. Unknown field names are skipped."""
    return {
        field_name_map[name]: val
        for name, val in values.items()
        if name in field_name_map and val not in (None, "", [], {})
    }


def map_model_to_acf(model: dict) -> dict:
    """
    Convert one zigwheels.ae-scraped model dict -> WordPress ACF payload.

    Model post (cars-data):
      - Cars Overview fields (price range, body/fuel/transmission, mileage)
      - Key Specifications fields (engine_cc, max_torque, no_of_airbags, …)
      - gallery_images (wp_uploader resolves to attachment IDs)
      - FAQs + similar-cars + colours passed through for optional custom fields

    Variant posts (car-variant):
      - Specifications/Features repeaters, built per-variant from
        zigwheels.ae's variant-comparison table + variant feature-delta
        cards (falls back to the shared model-level specs if a variant has
        no data of its own — e.g. single-variant models)
      - Basic variant info (name, price, transmission, engine_cc, power)

    Returns REST-ready payloads keyed by ACF field *names*:
      acf_model_payload    — {field_name: value} for cars-data posts
      acf_variant_payload  — {repeater_name: [{spec_name, spec_value}, …]} per variant
    """
    if "error" in model:
        return {
            "post_title": f"{model.get('brand', '')} {model.get('model', '')}".strip(),
            "error": model["error"],
            "source_url": model.get("url", ""),
            "variants": [],
        }

    brand    = model.get("brand", "")
    mdl_name = model.get("model", "")
    post_title = model.get("post_title") or f"{brand} {mdl_name}".strip()

    variants = model.get("variants", [])
    colours = model.get("colours", [])
    model_specs = _strip_colour_noise(model.get("specifications", []), colours)

    model_overview = _extract_overview(model)
    key_specs_acf  = _extract_key_specs(model, model_specs)
    model_overview["mileage"] = _extract_fuel_consumption(model, model_specs)
    whats_new_acf = _build_whats_new_acf(model)
    latest_updates_acf = _build_latest_updates_acf(model)

    acf_overview_rest = {
        name: val for name, val in model_overview.items()
        if val not in (None, "", [], {})
    }
    acf_key_specs_rest = {
        name: val for name, val in key_specs_acf.items()
        if val not in (None, "", [], {})
    }

    intro_para = (model.get("price_overview_text") or model.get("spec_overview_text") or "").strip()

    gallery = model.get("gallery_images") or {}
    all_gallery_urls = (
        list(gallery.get("exterior", [])) + list(gallery.get("interior", [])) + list(gallery.get("color", []))
    )
    featured_image_url = all_gallery_urls[0] if all_gallery_urls else ""

    # ---- Per-variant payloads --------------------------------------------
    # Each variant carries its OWN specifications/features (from the
    # zigwheels.ae variant-comparison table + variant feature-delta cards).
    #
    # FIX (2026-08-13) — Bug 10: the shared model-level "specifications"
    # list (built from the Review accordion's Core Dimensions / Engine /
    # Safety / Exterior sub-sections — the richest, most detailed source
    # the scraper produces) used to be used ONLY as a fallback for variants
    # that had *neither* their own specs nor features. In practice nearly
    # every multi-variant model DOES have its own (boilerplate) features
    # list from the "Compare Variants" table, so that fallback almost never
    # ran and the entire model-level specifications list — real engine
    # figures, dimensions, top speed, fuel efficiency, colours, detailed
    # safety/ADAS text — was silently discarded for every model with more
    # than one variant. Now the model-level specs are always merged in
    # underneath each variant's own data (variant-level values win on
    # conflicts since _build_spec_repeaters keeps the first row per choice
    # key and we build the variant's own rows first).
    mapped_variants = []
    for v in variants:
        v_specs = _strip_colour_noise(v.get("specifications") or [], colours)
        v_feats = v.get("features") or []
        v_repeaters = _build_spec_repeaters(v_specs, v_feats)
        model_repeaters = _build_spec_repeaters(model_specs, [])
        for group_name, rows in model_repeaters.items():
            existing = v_repeaters.setdefault(group_name, [])
            have = {r.get("spec_name") for r in existing}
            for row in rows:
                if row.get("spec_name") not in have:
                    existing.append(row)
                    have.add(row.get("spec_name"))

        acf_variant_payload = {group_name: rows for group_name, rows in v_repeaters.items() if rows}

        mapped_variants.append({
            "variant_name":        v.get("variant_name", ""),
            "variant_url":         v.get("variant_url", ""),
            "price_aed":           v.get("price_aed", ""),
            "price_display":       v.get("price_display", ""),
            "transmission":        v.get("transmission", ""),
            "engine_cc":           v.get("engine_cc", ""),
            "power":               v.get("power", ""),
            "spec_summary":        v.get("spec_summary", ""),
            "parent_model_name":   mdl_name,
            "acf_repeaters":       v_repeaters,
            "acf_variant_payload": acf_variant_payload,
        })

    return {
        "post_title":           post_title,
        "post_type":            "cars-data",
        "slug":                 _to_wp_slug(post_title),
        "post_content":         intro_para,
        "source_url":           model.get("url", ""),
        "brand":                brand,
        "model":                mdl_name,
        "model_year":           model.get("model_year", ""),
        "body_type":            model_overview.get("body_type", ""),
        "fuel_type":            model_overview.get("fuel_type", ""),
        "price_min_aed":        model.get("price_min_aed", ""),
        "price_max_aed":        model.get("price_max_aed", ""),
        "price_display":        model.get("price_display", ""),
        "monthly_installment_aed": model.get("monthly_installment_aed", ""),
        "price_city":           model.get("price_city", ""),
        "rating":               model.get("rating", ""),
        "review_count":         model.get("review_count", ""),
        "colours":              model.get("colours", []),
        "faqs":                 model.get("faqs", []),
        "similar_cars":         model.get("similar_cars", []),
        "featured_image_url":   featured_image_url,
        "gallery_images":       model.get("gallery_local_paths") or all_gallery_urls,
        # ACF for model post — by field name (REST-ready)
        "acf_overview":         model_overview,
        "acf_key_specs":        key_specs_acf,
        "acf_whats_new":        whats_new_acf,
        "acf_latest_updates":   latest_updates_acf,
        "acf_overview_rest":    acf_overview_rest,
        "acf_key_specs_rest":   acf_key_specs_rest,
        "acf_model_payload":    {**acf_overview_rest, **acf_key_specs_rest, **whats_new_acf, **latest_updates_acf},
        "variants":             mapped_variants,
    }


def map_all_models(scraped_data: list) -> list:
    """Map a full scraped JSON list (all models) to ACF payloads."""
    return [map_model_to_acf(m) for m in scraped_data]


def _load_wp_choice_sets(acf_export_path: Path) -> dict[str, set[str]]:
    """
    Read a WP ACF-export JSON (Tools > Export in ACF admin) and return
    {repeater_field_name: {valid_choice_key, …}} for every select sub-field
    inside a repeater — i.e. the actual set of values WP will currently
    accept for each Specifications/Features repeater's spec_name field.
    """
    with open(acf_export_path, encoding="utf-8-sig") as f:
        groups = json.load(f)
    out: dict[str, set[str]] = {}
    for group in groups:
        for field in group.get("fields", []):
            if field.get("type") != "repeater":
                continue
            for sub in field.get("sub_fields", []):
                if sub.get("type") == "select":
                    out[field.get("name")] = set(sub.get("choices", {}).keys())
    return out


def validate_against_wp(acf_data: list, acf_export_path: Path) -> None:
    """
    Cross-check every spec_name this run actually emitted against the real
    WP ACF choice lists. Anything the mapper produces that ISN'T a valid WP
    choice key will silently fail to save via the REST API (select fields
    reject unknown values) — print those out explicitly so it's obvious
    which ACF choices need to be added in WP Admin, instead of discovering
    it later as "data missing on the site" with no clue why.
    """
    if not acf_export_path.exists():
        return
    wp_choices = _load_wp_choice_sets(acf_export_path)

    missing: dict[str, set[str]] = {}
    for model in acf_data:
        for v in model.get("variants", []):
            for group_name, rows in v.get("acf_repeaters", {}).items():
                valid = wp_choices.get(group_name)
                if valid is None:
                    continue  # repeater not in export / not a select-based one
                for row in rows:
                    key = row.get("spec_name")
                    if key and key not in valid:
                        missing.setdefault(group_name, set()).add(key)

    if not missing:
        print("  ACF choice check: all emitted spec/feature keys exist in WP  ✓")
        return

    print("\n  WARNING: this run produced spec_name values that are NOT valid")
    print("  choices in the current WP export — these rows will be silently")
    print("  dropped/rejected by the ACF REST API until you add them in")
    print("  WP Admin → Custom Fields → the matching field group's repeater")
    print("  → its \"Spec Name\" / feature select field → Choices:\n")
    for group_name, keys in sorted(missing.items()):
        print(f"    {group_name}:")
        for k in sorted(keys):
            print(f"      - {k}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Map zigwheels.ae scraped JSON -> WordPress ACF payload",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input_json",  help="Scraped JSON file from scrapper.py")
    parser.add_argument("--output",    default="", help="Output ACF JSON file (default: <stem>_acf.json)")
    parser.add_argument("--acf-export", default="",
                         help="Path to a WP ACF-export JSON (ACF > Tools > Export) to validate "
                              "emitted spec_name/feature keys against the site's real choice lists")
    args = parser.parse_args()

    in_path = Path(args.input_json)
    if not in_path.exists():
        print(f"ERROR: Input file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading scraped data from: {in_path}")
    with open(in_path, encoding="utf-8") as f:
        scraped = json.load(f)

    if isinstance(scraped, dict):
        scraped = [scraped]

    print(f"Mapping {len(scraped)} car model(s) to ACF fields…")
    acf_data = map_all_models(scraped)

    out_path = Path(args.output) if args.output else in_path.with_name(in_path.stem + "_acf.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(acf_data, f, ensure_ascii=False, indent=2)

    print(f"ACF payload saved -> {out_path.resolve()}")
    print(f"  Models mapped   : {len(acf_data)}")
    total_variants = sum(len(m.get("variants", [])) for m in acf_data)
    print(f"  Total variants  : {total_variants}")

    total_rows = 0
    for m in acf_data:
        for v in m.get("variants", []):
            for rows in v.get("acf_repeaters", {}).values():
                total_rows += len(rows)
    print(f"  Total repeater rows mapped: {total_rows}")
    if total_rows == 0:
        print("  WARNING: 0 repeater rows — check that scraped specifications are not empty.")

    acf_export_path = Path(args.acf_export) if args.acf_export else in_path.parent / "acf-export-2026-08-13.json"
    validate_against_wp(acf_data, acf_export_path)


if __name__ == "__main__":
    main()