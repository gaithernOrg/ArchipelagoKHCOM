"""
Parses the WitnessLogic.txt logic file into useful data structures.
This is the heart of the randomization.

In WitnessLogic.txt we have regions defined with their connections:

Region Name (Short name) - Connected Region 1 - Connection Requirement 1 - Connected Region 2...

And then panels in that region with the hex code used in the game
previous panels that are required to turn them on, as well as the symbols they require:

0x##### (Panel Name) - Required Panels - Required Items

On __init__, the base logic is read and all panels are given Location IDs.
When the world has parsed its options, a second function is called to finalize the logic.
"""

import copy
from collections import defaultdict
from typing import cast, TYPE_CHECKING
from logging import warning

from .static_logic import StaticWitnessLogic, DoorItemDefinition, ItemCategory, ProgressiveItemDefinition
from .utils import *

if TYPE_CHECKING:
    from . import WitnessWorld


class WitnessPlayerLogic:
    """WITNESS LOGIC CLASS"""

    @lru_cache(maxsize=None)
    def reduce_req_within_region(self, panel_hex: str) -> FrozenSet[FrozenSet[str]]:
        """
        Panels in this game often only turn on when other panels are solved.
        Those other panels may have different item requirements.
        It would be slow to recursively check solvability each time.
        This is why we reduce the item dependencies within the region.
        Panels outside of the same region will still be checked manually.
        """

        if panel_hex in self.COMPLETELY_DISABLED_ENTITIES or panel_hex in self.IRRELEVANT_BUT_NOT_DISABLED_ENTITIES:
            return frozenset()

        entity_obj = self.REFERENCE_LOGIC.ENTITIES_BY_HEX[panel_hex]

        these_items = frozenset({frozenset()})

        if entity_obj["id"]:
            these_items = self.DEPENDENT_REQUIREMENTS_BY_HEX[panel_hex]["items"]

        these_items = frozenset({
            subset.intersection(self.THEORETICAL_ITEMS_NO_MULTI)
            for subset in these_items
        })

        for subset in these_items:
            self.PROG_ITEMS_ACTUALLY_IN_THE_GAME_NO_MULTI.update(subset)

        these_panels = self.DEPENDENT_REQUIREMENTS_BY_HEX[panel_hex]["panels"]

        if panel_hex in self.DOOR_ITEMS_BY_ID:
            door_items = frozenset({frozenset([item]) for item in self.DOOR_ITEMS_BY_ID[panel_hex]})

            all_options = set()

            for dependentItem in door_items:
                self.PROG_ITEMS_ACTUALLY_IN_THE_GAME_NO_MULTI.update(dependentItem)
                for items_option in these_items:
                    all_options.add(items_option.union(dependentItem))

            # 0x28A0D depends on another entity for *non-power* reasons -> This dependency needs to be preserved,
            # except in Expert, where that dependency doesn't exist, but now there *is* a power dependency.
            # In the future, it would be wise to make a distinction between "power dependencies" and other dependencies.
            if panel_hex == "0x28A0D" and not any("0x28998" in option for option in these_panels):
                these_items = all_options

            # Another dependency that is not power-based: The Symmetry Island Upper Panel latches
            elif panel_hex == "0x1C349":
                these_items = all_options

            # For any other door entity, we just return a set with the item that opens it & disregard power dependencies
            else:
                return frozenset(all_options)

        disabled_eps = {eHex for eHex in self.COMPLETELY_DISABLED_ENTITIES
                        if self.REFERENCE_LOGIC.ENTITIES_BY_HEX[eHex]["entityType"] == "EP"}

        these_panels = frozenset({panels - disabled_eps
                                  for panels in these_panels})

        if these_panels == frozenset({frozenset()}):
            return these_items

        all_options = set()

        for option in these_panels:
            dependent_items_for_option = frozenset({frozenset()})

            for option_entity in option:
                dep_obj = self.REFERENCE_LOGIC.ENTITIES_BY_HEX.get(option_entity)

                if option_entity in self.EVENT_NAMES_BY_HEX:
                    new_items = frozenset({frozenset([option_entity])})
                elif option_entity in {"7 Lasers", "11 Lasers", "7 Lasers + Redirect", "11 Lasers + Redirect",
                                       "PP2 Weirdness", "Theater to Tunnels"}:
                    new_items = frozenset({frozenset([option_entity])})
                else:
                    new_items = self.reduce_req_within_region(option_entity)
                    if dep_obj["region"] and entity_obj["region"] != dep_obj["region"]:
                        new_items = frozenset(
                            frozenset(possibility | {dep_obj["region"]["name"]})
                            for possibility in new_items
                        )

                dependent_items_for_option = dnf_and([dependent_items_for_option, new_items])

            for items_option in these_items:
                for dependentItem in dependent_items_for_option:
                    all_options.add(items_option.union(dependentItem))

        return dnf_remove_redundancies(frozenset(all_options))

    def make_single_adjustment(self, adj_type: str, line: str):
        from . import StaticWitnessItems
        """Makes a single logic adjustment based on additional logic file"""

        if adj_type == "Items":
            line_split = line.split(" - ")
            item_name = line_split[0]

            if item_name not in StaticWitnessItems.item_data:
                raise RuntimeError("Item \"" + item_name + "\" does not exist.")

            self.THEORETICAL_ITEMS.add(item_name)
            if isinstance(StaticWitnessLogic.all_items[item_name], ProgressiveItemDefinition):
                self.THEORETICAL_ITEMS_NO_MULTI.update(cast(ProgressiveItemDefinition,
                                                            StaticWitnessLogic.all_items[item_name]).child_item_names)
            else:
                self.THEORETICAL_ITEMS_NO_MULTI.add(item_name)

            if StaticWitnessLogic.all_items[item_name].category in [ItemCategory.DOOR, ItemCategory.LASER]:
                panel_hexes = cast(DoorItemDefinition, StaticWitnessLogic.all_items[item_name]).panel_id_hexes
                for panel_hex in panel_hexes:
                    self.DOOR_ITEMS_BY_ID.setdefault(panel_hex, []).append(item_name)

            return

        if adj_type == "Remove Items":
            item_name = line

            self.THEORETICAL_ITEMS.discard(item_name)
            if isinstance(StaticWitnessLogic.all_items[item_name], ProgressiveItemDefinition):
                self.THEORETICAL_ITEMS_NO_MULTI.difference_update(
                    cast(ProgressiveItemDefinition, StaticWitnessLogic.all_items[item_name]).child_item_names
                )
            else:
                self.THEORETICAL_ITEMS_NO_MULTI.discard(item_name)

            if StaticWitnessLogic.all_items[item_name].category in [ItemCategory.DOOR, ItemCategory.LASER]:
                panel_hexes = cast(DoorItemDefinition, StaticWitnessLogic.all_items[item_name]).panel_id_hexes
                for panel_hex in panel_hexes:
                    if panel_hex in self.DOOR_ITEMS_BY_ID and item_name in self.DOOR_ITEMS_BY_ID[panel_hex]:
                        self.DOOR_ITEMS_BY_ID[panel_hex].remove(item_name)

        if adj_type == "Starting Inventory":
            self.STARTING_INVENTORY.add(line)

        if adj_type == "Event Items":
            line_split = line.split(" - ")
            new_event_name = line_split[0]
            hex_set = line_split[1].split(",")

            for entity, event_name in self.EVENT_NAMES_BY_HEX.items():
                if event_name == new_event_name:
                    self.DONT_MAKE_EVENTS.add(entity)

            for hex_code in hex_set:
                self.EVENT_NAMES_BY_HEX[hex_code] = new_event_name

            return

        if adj_type == "Requirement Changes":
            line_split = line.split(" - ")

            requirement = {
                "panels": parse_lambda(line_split[1]),
            }

            if len(line_split) > 2:
                required_items = parse_lambda(line_split[2])
                items_actually_in_the_game = [
                    item_name for item_name, item_definition in StaticWitnessLogic.all_items.items()
                    if item_definition.category is ItemCategory.SYMBOL
                ]
                required_items = frozenset(
                    subset.intersection(items_actually_in_the_game)
                    for subset in required_items
                )

                requirement["items"] = required_items

            self.DEPENDENT_REQUIREMENTS_BY_HEX[line_split[0]] = requirement

            return

        if adj_type == "Disabled Locations":
            panel_hex = line[:7]

            self.COMPLETELY_DISABLED_ENTITIES.add(panel_hex)

            return

        if adj_type == "Irrelevant Locations":
            panel_hex = line[:7]

            self.IRRELEVANT_BUT_NOT_DISABLED_ENTITIES.add(panel_hex)

            return

        if adj_type == "Region Changes":
            new_region_and_options = define_new_region(line + ":")

            self.CONNECTIONS_BY_REGION_NAME[new_region_and_options[0]["name"]] = new_region_and_options[1]

            return

        if adj_type == "New Connections":
            line_split = line.split(" - ")
            source_region = line_split[0]
            target_region = line_split[1]
            panel_set_string = line_split[2]

            for connection in self.CONNECTIONS_BY_REGION_NAME[source_region]:
                if connection[0] == target_region:
                    self.CONNECTIONS_BY_REGION_NAME[source_region].remove(connection)

                    if panel_set_string == "TrueOneWay":
                        self.CONNECTIONS_BY_REGION_NAME[source_region].add(
                            (target_region, frozenset({frozenset(["TrueOneWay"])}))
                        )
                    else:
                        new_lambda = connection[1] | parse_lambda(panel_set_string)
                        self.CONNECTIONS_BY_REGION_NAME[source_region].add((target_region, new_lambda))
                    break
            else:  # Execute if loop did not break. TIL this is a thing you can do!
                new_conn = (target_region, parse_lambda(panel_set_string))
                self.CONNECTIONS_BY_REGION_NAME[source_region].add(new_conn)

        if adj_type == "Added Locations":
            if "0x" in line:
                line = self.REFERENCE_LOGIC.ENTITIES_BY_HEX[line]["checkName"]
            self.ADDED_CHECKS.add(line)

    @staticmethod
    def handle_postgame(world: "WitnessWorld"):
        # In shuffle_postgame, panels that become accessible "after or at the same time as the goal" are disabled.
        # This has a lot of complicated considerations, which I'll try my best to explain.
        postgame_adjustments = []

        # Make some quick references to some options
        doors = world.options.shuffle_doors >= 2  # "Panels" mode has no overarching region accessibility implications.
        early_caves = world.options.early_caves
        victory = world.options.victory_condition
        mnt_lasers = world.options.mountain_lasers
        chal_lasers = world.options.challenge_lasers

        # Goal is "short box" but short box requires more lasers than long box
        reverse_shortbox_goal = victory == "mountain_box_short" and mnt_lasers > chal_lasers

        # Goal is "short box", and long box requires at least as many lasers as short box (as god intended)
        proper_shortbox_goal = victory == "mountain_box_short" and chal_lasers >= mnt_lasers

        # Goal is "long box", but short box requires at least as many lasers than long box.
        reverse_longbox_goal = victory == "mountain_box_long" and mnt_lasers >= chal_lasers

        # If goal is shortbox or "reverse longbox", you will never enter the mountain from the top before winning.
        mountain_enterable_from_top = not (victory == "mountain_box_short" or reverse_longbox_goal)

        # Caves & Challenge should never have anything if doors are vanilla - definitionally "post-game"
        # This is technically imprecise, but it matches player expectations better.
        if not (early_caves or doors):
            postgame_adjustments.append(get_caves_exclusion_list())
            postgame_adjustments.append(get_beyond_challenge_exclusion_list())

            # If Challenge is the goal, some panels on the way need to be left on, as well as Challenge Vault box itself
            if not victory == "challenge":
                postgame_adjustments.append(get_path_to_challenge_exclusion_list())
                postgame_adjustments.append(get_challenge_vault_box_exclusion_list())

        # Challenge can only have something if the goal is not challenge or longbox itself.
        # In case of shortbox, it'd have to be a "reverse shortbox" situation where shortbox requires *more* lasers.
        # In that case, it'd also have to be a doors mode, but that's already covered by the previous block.
        if not (victory == "elevator" or reverse_shortbox_goal):
            postgame_adjustments.append(get_beyond_challenge_exclusion_list())
            if not victory == "challenge":
                postgame_adjustments.append(get_challenge_vault_box_exclusion_list())

        # Mountain can't be reached if the goal is shortbox (or "reverse long box")
        if not mountain_enterable_from_top:
            postgame_adjustments.append(get_mountain_upper_exclusion_list())

            # Same goes for lower mountain, but that one *can* be reached in remote doors modes.
            if not doors:
                postgame_adjustments.append(get_mountain_lower_exclusion_list())

        # The Mountain Bottom Floor Discard is a bit complicated, so we handle it separately. ("it" == the Discard)
        # In Elevator Goal, it is definitionally in the post-game, unless remote doors is played.
        # In Challenge Goal, it is before the Challenge, so it is not post-game.
        # In Short Box Goal, you can win before turning it on, UNLESS Short Box requires MORE lasers than long box.
        # In Long Box Goal, it is always in the post-game because solving long box is what turns it on.
        if not ((victory == "elevator" and doors) or victory == "challenge" or (reverse_shortbox_goal and doors)):
            # We now know Bottom Floor Discard is in the post-game.
            # This has different consequences depending on whether remote doors is being played.
            # If doors are vanilla, Bottom Floor Discard locks a door to an area, which has to be disabled as well.
            if doors:
                postgame_adjustments.append(get_bottom_floor_discard_exclusion_list())
            else:
                postgame_adjustments.append(get_bottom_floor_discard_nondoors_exclusion_list())

        # In Challenge goal + early_caves + vanilla doors, you could find something important on Bottom Floor Discard,
        # including the Caves Shortcuts themselves if playing "early_caves: start_inventory".
        # This is another thing that was deemed "unfun" more than fitting the actual definition of post-game.
        if victory == "challenge" and early_caves and not doors:
            postgame_adjustments.append(get_bottom_floor_discard_nondoors_exclusion_list())

        # If we have a proper short box goal, long box will never be activated first.
        if proper_shortbox_goal:
            postgame_adjustments.append(["Disabled Locations:", "0xFFF00 (Mountain Box Long)"])

        return postgame_adjustments

    def make_options_adjustments(self, world: "WitnessWorld"):
        """Makes logic adjustments based on options"""
        adjustment_linesets_in_order = []

        # Make condensed references to some options

        doors = world.options.shuffle_doors >= 2  # "Panels" mode has no overarching region accessibility implications.
        lasers = world.options.shuffle_lasers
        victory = world.options.victory_condition
        mnt_lasers = world.options.mountain_lasers
        chal_lasers = world.options.challenge_lasers

        # Exclude panels from the post-game if shuffle_postgame is false.
        if not world.options.shuffle_postgame:
            adjustment_linesets_in_order += self.handle_postgame(world)

        # Exclude Discards / Vaults
        if not world.options.shuffle_discarded_panels:
            # In disable_non_randomized, the discards are needed for alternate activation triggers, UNLESS both
            # (remote) doors and lasers are shuffled.
            if not world.options.disable_non_randomized_puzzles or (doors and lasers):
                adjustment_linesets_in_order.append(get_discard_exclusion_list())

            if doors:
                adjustment_linesets_in_order.append(get_bottom_floor_discard_exclusion_list())

        if not world.options.shuffle_vault_boxes:
            adjustment_linesets_in_order.append(get_vault_exclusion_list())
            if not victory == "challenge":
                adjustment_linesets_in_order.append(get_challenge_vault_box_exclusion_list())

        # Victory Condition

        if victory == "elevator":
            self.VICTORY_LOCATION = "0x3D9A9"
        elif victory == "challenge":
            self.VICTORY_LOCATION = "0x0356B"
        elif victory == "mountain_box_short":
            self.VICTORY_LOCATION = "0x09F7F"
        elif victory == "mountain_box_long":
            self.VICTORY_LOCATION = "0xFFF00"

        # Long box can usually only be solved by opening Mountain Entry. However, if it requires 7 lasers or less
        # (challenge_lasers <= 7), you can now solve it without opening Mountain Entry first.
        # Furthermore, if the user sets mountain_lasers > 7, the box is rotated to not require Mountain Entry either.
        if chal_lasers <= 7 or mnt_lasers > 7:
            adjustment_linesets_in_order.append([
                "Requirement Changes:",
                "0xFFF00 - 11 Lasers - True",
            ])

        if world.options.disable_non_randomized_puzzles:
            adjustment_linesets_in_order.append(get_disable_unrandomized_list())

        if world.options.shuffle_symbols:
            adjustment_linesets_in_order.append(get_symbol_shuffle_list())

        if world.options.EP_difficulty == "normal":
            adjustment_linesets_in_order.append(get_ep_easy())
        elif world.options.EP_difficulty == "tedious":
            adjustment_linesets_in_order.append(get_ep_no_eclipse())

        if world.options.door_groupings == "regional":
            if world.options.shuffle_doors == "panels":
                adjustment_linesets_in_order.append(get_simple_panels())
            elif world.options.shuffle_doors == "doors":
                adjustment_linesets_in_order.append(get_simple_doors())
            elif world.options.shuffle_doors == "mixed":
                adjustment_linesets_in_order.append(get_simple_doors())
                adjustment_linesets_in_order.append(get_simple_additional_panels())
        else:
            if world.options.shuffle_doors == "panels":
                adjustment_linesets_in_order.append(get_complex_door_panels())
                adjustment_linesets_in_order.append(get_complex_additional_panels())
            elif world.options.shuffle_doors == "doors":
                adjustment_linesets_in_order.append(get_complex_doors())
            elif world.options.shuffle_doors == "mixed":
                adjustment_linesets_in_order.append(get_complex_doors())
                adjustment_linesets_in_order.append(get_complex_additional_panels())

        if world.options.shuffle_boat:
            adjustment_linesets_in_order.append(get_boat())

        if world.options.early_caves == "starting_inventory":
            adjustment_linesets_in_order.append(get_early_caves_start_list())

        if world.options.early_caves == "add_to_pool" and not doors:
            adjustment_linesets_in_order.append(get_early_caves_list())

        if world.options.elevators_come_to_you:
            adjustment_linesets_in_order.append(get_elevators_come_to_you())

        for item in self.YAML_ADDED_ITEMS:
            adjustment_linesets_in_order.append(["Items:", item])

        if lasers:
            adjustment_linesets_in_order.append(get_laser_shuffle())

        if world.options.shuffle_EPs == "obelisk_sides":
            ep_gen = ((ep_hex, ep_obj) for (ep_hex, ep_obj) in self.REFERENCE_LOGIC.ENTITIES_BY_HEX.items()
                      if ep_obj["entityType"] == "EP")

            for ep_hex, ep_obj in ep_gen:
                obelisk = self.REFERENCE_LOGIC.ENTITIES_BY_HEX[self.REFERENCE_LOGIC.EP_TO_OBELISK_SIDE[ep_hex]]
                obelisk_name = obelisk["checkName"]
                ep_name = self.REFERENCE_LOGIC.ENTITIES_BY_HEX[ep_hex]["checkName"]
                self.EVENT_NAMES_BY_HEX[ep_hex] = f"{obelisk_name} - {ep_name}"
        else:
            adjustment_linesets_in_order.append(["Disabled Locations:"] + get_ep_obelisks()[1:])

        if not world.options.shuffle_EPs:
            adjustment_linesets_in_order.append(["Irrelevant Locations:"] + get_ep_all_individual()[1:])

        for yaml_disabled_location in self.YAML_DISABLED_LOCATIONS:
            if yaml_disabled_location not in self.REFERENCE_LOGIC.ENTITIES_BY_NAME:
                continue

            loc_obj = self.REFERENCE_LOGIC.ENTITIES_BY_NAME[yaml_disabled_location]

            if loc_obj["entityType"] == "EP":
                self.COMPLETELY_DISABLED_ENTITIES.add(loc_obj["entity_hex"])

            elif loc_obj["entityType"] in {"General", "Vault", "Discard"}:
                self.EXCLUDED_LOCATIONS.add(loc_obj["entity_hex"])

        for adjustment_lineset in adjustment_linesets_in_order:
            current_adjustment_type = None

            for line in adjustment_lineset:
                if len(line) == 0:
                    continue

                if line[-1] == ":":
                    current_adjustment_type = line[:-1]
                    continue

                self.make_single_adjustment(current_adjustment_type, line)

        for entity_id in self.COMPLETELY_DISABLED_ENTITIES:
            if entity_id in self.DOOR_ITEMS_BY_ID:
                del self.DOOR_ITEMS_BY_ID[entity_id]

    def make_dependency_reduced_checklist(self):
        """
        Turns dependent check set into semi-independent check set
        """

        for entity_hex in self.DEPENDENT_REQUIREMENTS_BY_HEX.keys():
            indep_requirement = self.reduce_req_within_region(entity_hex)

            self.REQUIREMENTS_BY_HEX[entity_hex] = indep_requirement

        for item in self.PROG_ITEMS_ACTUALLY_IN_THE_GAME_NO_MULTI:
            if item not in self.THEORETICAL_ITEMS:
                progressive_item_name = StaticWitnessLogic.get_parent_progressive_item(item)
                self.PROG_ITEMS_ACTUALLY_IN_THE_GAME.add(progressive_item_name)
                child_items = cast(ProgressiveItemDefinition,
                                   StaticWitnessLogic.all_items[progressive_item_name]).child_item_names
                multi_list = [child_item for child_item in child_items
                              if child_item in self.PROG_ITEMS_ACTUALLY_IN_THE_GAME_NO_MULTI]
                self.MULTI_AMOUNTS[item] = multi_list.index(item) + 1
                self.MULTI_LISTS[progressive_item_name] = multi_list
            else:
                self.PROG_ITEMS_ACTUALLY_IN_THE_GAME.add(item)

        for region, connections in self.CONNECTIONS_BY_REGION_NAME.items():
            new_connections = []

            for connection in connections:
                overall_requirement = frozenset()

                for option in connection[1]:
                    individual_entity_requirements = []
                    for entity in option:
                        if entity in self.EVENT_NAMES_BY_HEX or entity not in self.REFERENCE_LOGIC.ENTITIES_BY_HEX:
                            individual_entity_requirements.append(frozenset({frozenset({entity})}))
                        else:
                            entity_req = self.reduce_req_within_region(entity)

                            if self.REFERENCE_LOGIC.ENTITIES_BY_HEX[entity]["region"]:
                                region_name = self.REFERENCE_LOGIC.ENTITIES_BY_HEX[entity]["region"]["name"]
                                entity_req = dnf_and([entity_req, frozenset({frozenset({region_name})})])

                            individual_entity_requirements.append(entity_req)

                    overall_requirement |= dnf_and(individual_entity_requirements)

                new_connections.append((connection[0], overall_requirement))

            self.CONNECTIONS_BY_REGION_NAME[region] = new_connections

    def make_event_item_pair(self, panel: str):
        """
        Makes a pair of an event panel and its event item
        """
        action = " Opened" if self.REFERENCE_LOGIC.ENTITIES_BY_HEX[panel]["entityType"] == "Door" else " Solved"

        name = self.REFERENCE_LOGIC.ENTITIES_BY_HEX[panel]["checkName"] + action
        if panel not in self.EVENT_NAMES_BY_HEX:
            warning("Panel \"" + name + "\" does not have an associated event name.")
            self.EVENT_NAMES_BY_HEX[panel] = name + " Event"
        pair = (name, self.EVENT_NAMES_BY_HEX[panel])
        return pair

    def make_event_panel_lists(self):
        self.EVENT_NAMES_BY_HEX[self.VICTORY_LOCATION] = "Victory"

        for event_hex, event_name in self.EVENT_NAMES_BY_HEX.items():
            if event_hex in self.COMPLETELY_DISABLED_ENTITIES or event_hex in self.IRRELEVANT_BUT_NOT_DISABLED_ENTITIES:
                continue
            self.EVENT_PANELS.add(event_hex)

        for panel in self.EVENT_PANELS:
            pair = self.make_event_item_pair(panel)
            self.EVENT_ITEM_PAIRS[pair[0]] = pair[1]

    def __init__(self, world: "WitnessWorld", disabled_locations: Set[str], start_inv: Dict[str, int]):
        self.YAML_DISABLED_LOCATIONS = disabled_locations
        self.YAML_ADDED_ITEMS = start_inv

        self.EVENT_PANELS_FROM_PANELS = set()
        self.EVENT_PANELS_FROM_REGIONS = set()

        self.IRRELEVANT_BUT_NOT_DISABLED_ENTITIES = set()

        self.THEORETICAL_ITEMS = set()
        self.THEORETICAL_ITEMS_NO_MULTI = set()
        self.MULTI_AMOUNTS = defaultdict(lambda: 1)
        self.MULTI_LISTS = dict()
        self.PROG_ITEMS_ACTUALLY_IN_THE_GAME_NO_MULTI = set()
        self.PROG_ITEMS_ACTUALLY_IN_THE_GAME = set()
        self.DOOR_ITEMS_BY_ID: Dict[str, List[str]] = {}
        self.STARTING_INVENTORY = set()

        self.DIFFICULTY = world.options.puzzle_randomization

        if self.DIFFICULTY == "sigma_normal":
            self.REFERENCE_LOGIC = StaticWitnessLogic.sigma_normal
        elif self.DIFFICULTY == "sigma_expert":
            self.REFERENCE_LOGIC = StaticWitnessLogic.sigma_expert
        elif self.DIFFICULTY == "none":
            self.REFERENCE_LOGIC = StaticWitnessLogic.vanilla

        self.CONNECTIONS_BY_REGION_NAME = copy.copy(self.REFERENCE_LOGIC.STATIC_CONNECTIONS_BY_REGION_NAME)
        self.DEPENDENT_REQUIREMENTS_BY_HEX = copy.copy(self.REFERENCE_LOGIC.STATIC_DEPENDENT_REQUIREMENTS_BY_HEX)
        self.REQUIREMENTS_BY_HEX = dict()

        # Determining which panels need to be events is a difficult process.
        # At the end, we will have EVENT_ITEM_PAIRS for all the necessary ones.
        self.EVENT_PANELS = set()
        self.EVENT_ITEM_PAIRS = dict()
        self.DONT_MAKE_EVENTS = set()
        self.COMPLETELY_DISABLED_ENTITIES = set()
        self.PRECOMPLETED_LOCATIONS = set()
        self.EXCLUDED_LOCATIONS = set()
        self.ADDED_CHECKS = set()
        self.VICTORY_LOCATION = "0x0356B"

        self.EVENT_NAMES_BY_HEX = {
            "0x00509": "+1 Laser (Symmetry Laser)",
            "0x012FB": "+1 Laser (Desert Laser)",
            "0x09F98": "Desert Laser Redirection",
            "0x01539": "+1 Laser (Quarry Laser)",
            "0x181B3": "+1 Laser (Shadows Laser)",
            "0x014BB": "+1 Laser (Keep Laser)",
            "0x17C65": "+1 Laser (Monastery Laser)",
            "0x032F9": "+1 Laser (Town Laser)",
            "0x00274": "+1 Laser (Jungle Laser)",
            "0x0C2B2": "+1 Laser (Bunker Laser)",
            "0x00BF6": "+1 Laser (Swamp Laser)",
            "0x028A4": "+1 Laser (Treehouse Laser)",
            "0x09F7F": "Mountain Entry",
            "0xFFF00": "Bottom Floor Discard Turns On",
        }

        self.make_options_adjustments(world)
        self.make_dependency_reduced_checklist()
        self.make_event_panel_lists()
