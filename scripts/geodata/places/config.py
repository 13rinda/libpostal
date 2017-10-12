import copy
import operator
import os
import random
import six
import yaml

from collections import defaultdict, OrderedDict

from geodata.addresses.dependencies import ComponentDependencies
from geodata.address_expansions.address_dictionaries import address_phrase_dictionaries
from geodata.address_formatting.formatter import AddressFormatter
from geodata.configs.utils import nested_get, recursive_merge, RESOURCES_DIR
from geodata.math.floats import isclose
from geodata.math.sampling import cdf, weighted_choice

from geodata.encoding import safe_encode

this_dir = os.path.realpath(os.path.dirname(__file__))

PLACE_CONFIG_FILE = os.path.join(RESOURCES_DIR, 'places', 'countries', 'global.yaml')


class PlaceConfig(object):
    ADMIN_COMPONENTS = {
        AddressFormatter.LOCALITY,
        AddressFormatter.SUBURB,
        AddressFormatter.CITY_DISTRICT,
        AddressFormatter.CITY,
        AddressFormatter.ISLAND,
        AddressFormatter.STATE_DISTRICT,
        AddressFormatter.STATE,
        AddressFormatter.COUNTRY_REGION,
        AddressFormatter.COUNTRY,
        AddressFormatter.WORLD_REGION,
        AddressFormatter.POSTCODE,
    }

    numeric_ops = {'lte': operator.le,
                   'gt': operator.gt,
                   'lt': operator.lt,
                   'gte': operator.ge,
                   }

    def __init__(self, config_file=PLACE_CONFIG_FILE):
        self.cache = {}
        place_config = yaml.load(open(config_file))

        self.global_config = place_config['global']
        self.country_configs = {}

        self.cdf_cache = {}

        countries = place_config.pop('countries', {})

        for k, v in six.iteritems(countries):
            country_config = countries[k]
            global_config_copy = copy.deepcopy(self.global_config)
            self.country_configs[k] = recursive_merge(global_config_copy, country_config)

        self.country_configs[None] = self.global_config

        self.setup_component_dependencies()

    def setup_component_dependencies(self):
        self.component_dependencies = {}

        for country, conf in six.iteritems(self.country_configs):
            graph = {k: c['dependencies'] for k, c in six.iteritems(conf['components']) if 'dependencies' in c}
            graph.update({c: [] for c in self.ADMIN_COMPONENTS if c not in graph})

            self.component_dependencies[country] = ComponentDependencies(graph)

            conf_graphs = {}

            for k, v in six.iteritems(conf['components']):
                for conf in v.get('containing', []):
                    if 'dependencies' in conf:
                        elem_type = conf['type']
                        elem_id = safe_encode(conf['id'])

                        conf_graph = conf_graphs.get((elem_type, elem_id))
                        if not conf_graph:
                            conf_graphs[(elem_type, elem_id)] = conf_graph = graph.copy()

                        conf_graph[k] = conf['dependencies']

            self.component_dependencies.update({k: ComponentDependencies(v) for k, v in six.iteritems(conf_graphs)})

    def get_property(self, key, country=None, default=None):
        if isinstance(key, six.string_types):
            key = key.split('.')

        config = self.global_config

        if country:
            country_config = self.country_configs.get(country.lower(), {})
            if country_config:
                config = country_config

        return nested_get(config, key, default=default)

    def include_by_population_exceptions(self, population_exceptions, population):
        if population_exceptions:
            try:
                population = int(population)
            except (TypeError, ValueError):
                population = 0

            for exc in population_exceptions:
                support = 0

                for k in exc:
                    op = self.numeric_ops.get(k)
                    if not op:
                        continue
                    res = op(population, exc[k])
                    if not res:
                        support = 0
                        break

                    support += 1

                if support > 0:
                    probability = exc.get('probability', 0.0)
                    if random.random() < probability:
                        return True
        return False

    def include_component_simple(self, component, containing_ids, country=None):
        containing = self.get_property(('components', component, 'containing'), country=country, default=None)

        if containing is not None:
            for c in containing:
                if (c['type'], safe_encode(c['id'])) in containing_ids:
                    return random.random() < c['probability']

        probability = self.get_property(('components', component, 'probability'), country=country, default=0.0)

        return random.random() < probability

    def include_component(self, component, containing_ids, country=None, population=None, check_population=True, unambiguous_city=False, have_postcode=False):
        if check_population and not unambiguous_city:
            population_exceptions = self.get_property(('components', component, 'population'), country=country, default=None)
            if population is None:
                if have_postcode:
                    have_postcode_prob = self.get_property(('components', component, 'have_postcode_probability'), country=country, default=None)
                    if have_postcode_prob is not None:
                        return random.random() < float(have_postcode_prob)
                population_unknown_prob = self.get_property(('components', component, 'population_unknown_probability'), country=country, default=None)
                if population_unknown_prob is not None:
                    return random.random() < float(population_unknown_prob)
            if population_exceptions and self.include_by_population_exceptions(population_exceptions, population=population or 0):
                return True
        return self.include_component_simple(component, containing_ids, country=country)

    def drop_invalid_components(self, address_components, country, containing_ids=(), original_bitset=None):
        if not address_components:
            return
        component_bitset = ComponentDependencies.component_bitset(address_components)

        for object_type, object_id in containing_ids:
            deps = self.component_dependencies.get((object_type, object_id))
            if deps:
                break
        else:
            deps = self.component_dependencies.get(country, self.component_dependencies[None])
        dep_order = deps.dependency_order

        for c in dep_order:
            if c not in address_components:
                continue
            if c in deps and not component_bitset & deps[c] and (original_bitset is None or not original_bitset & deps[c]):
                address_components.pop(c)
                component_bitset ^= ComponentDependencies.component_bit_values[c]

    def country_uses_locality_and_city(self, country):
        locality_and_city_prob = float(self.get_property(('locality_and_city_probability',), country=country, default=0.0))
        return not isclose(locality_and_city_prob, 0.0)

    def city_replacements(self, country):
        return OrderedDict.fromkeys(self.get_property(('city_replacements', ), country=country))

    def dropout_components(self, components, boundaries=(), country=None, population=None, unambiguous_city=False):
        containing_ids = set()

        for boundary in boundaries:
            object_type = boundary.get('type')
            object_id = safe_encode(boundary.get('id', ''))
            if not (object_type and object_id):
                continue
            containing_ids.add((object_type, object_id))

        original_bitset = ComponentDependencies.component_bitset(components)

        names = defaultdict(list)
        admin_components = [c for c in components if c in self.ADMIN_COMPONENTS]
        for c in admin_components:
            names[components[c]].append(c)

        same_name = set()
        for c, v in six.iteritems(names):
            if len(v) > 1:
                same_name |= set(v)

        new_components = components.copy()

        city_replacements = OrderedDict()
        if AddressFormatter.CITY not in components:
            city_replacements = self.city_replacements(country)

        for component in admin_components:
            include = self.include_component(component, containing_ids, country=country, population=population, unambiguous_city=unambiguous_city)

            if not include:
                # Note: this check is for cities that have the same name as their admin
                # areas e.g. Luxembourg, Luxembourg. In cases like this, if we were to drop
                # city, we don't want to include country on its own. This should help the parser
                # default to the city in ambiguous cases where only one component is specified.
                if not (component == AddressFormatter.CITY and component in same_name):
                    new_components.pop(component, None)
                else:
                    value = components[component]
                    for c in names[value]:
                        new_components.pop(c, None)

        for component in self.ADMIN_COMPONENTS:
            value = self.get_property(('components', component, 'value'), country=country, default=None)

            if not value:
                values, probs = self.cdf_cache.get((country, component), (None, None))
                if values is None:
                    values = self.get_property(('components', component, 'values'), country=country, default=None)
                    if values is not None:
                        values, probs = zip(*[(v['value'], float(v['probability'])) for v in values])
                        probs = cdf(probs)
                        self.cdf_cache[(country, component)] = (values, probs)

                if values is not None:
                    value = weighted_choice(values, probs)

            if value is not None and component not in components and self.include_component(component, containing_ids, country=country, population=population, unambiguous_city=unambiguous_city):
                new_components[component] = value

        self.drop_invalid_components(new_components, country, containing_ids=containing_ids, original_bitset=original_bitset)

        if AddressFormatter.LOCALITY in new_components:
            replace_city_with_locality_prob = float(self.get_property(('replace_city_with_locality_probability',), country=country, default=0.0))
            locality_and_city_prob = float(self.get_property(('locality_and_city_probability',), country=country, default=0.0))

            locality = new_components[AddressFormatter.LOCALITY]

            if random.random() < replace_city_with_locality_prob:
                new_components.pop(AddressFormatter.LOCALITY, None)
                new_components[AddressFormatter.CITY] = locality
            elif random.random() > locality_and_city_prob:
                new_components.pop(AddressFormatter.LOCALITY, None)

        if AddressFormatter.CITY not in new_components and not any((c in city_replacements for c in new_components)):
            city = components.get(AddressFormatter.CITY)
            if city:
                new_components[AddressFormatter.CITY] = city
            else:
                for c in city_replacements.keys():
                    val = components.get(c)
                    if val:
                        new_components[c] = val
                        break

        return new_components


place_config = PlaceConfig()
