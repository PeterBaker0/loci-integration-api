import asyncio
import asyncpg
from decimal import Decimal
from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientConnectorError
from config import TRIPLESTORE_CACHE_SPARQL_ENDPOINT
from config import GEOBASE_ENDPOINT 
from config import ES_ENDPOINT

from json import loads

from errors import ReportableAPIError

#Until we have a better way of understanding fundamental units in spatial hierarchies
prefix_base_unit_lookup = {
"linked.data.gov.au/dataset/asgs2016" : "linked.data.gov.au/dataset/asgs2016/meshblock",
"linked.data.gov.au/dataset/geofabric" : "linked.data.gov.au/dataset/geofabric/contractedcatchment"
}


async def get_to_base_unit_and_type_prefix(from_uri, query_uri):
    '''
    Find the base_unit and type prefix of a uri that is not of the from_uri type
    and is of the query_uri type
    '''
    base_unit_prefix = None
    resource_type_prefix = None
    for key, value in prefix_base_unit_lookup.items():
        if not key in from_uri:
            if key in query_uri:
                base_unit_prefix = value 
                resource_type_prefix = key 
                return base_unit_prefix, resource_type_prefix
    return base_unit_prefix, resource_type_prefix

async def get_all_overlaps(target_uri, include_contains=True, include_within=True):
    offset = 0
    all_overlaps = []
    while True:
        results = list(await get_location_overlaps(target_uri, None, True, True, include_within, include_contains, count=100000, offset=offset))
        length = results[0]['count']
        my_area = results[0]['featureArea'] 
        all_overlaps = all_overlaps + results[1]
        if length < 100000:
            break
        offset += 100000
    return my_area, all_overlaps

async def query_graphdb_endpoint(sparql, infer=True, same_as=True, limit=1000, offset=0):
    """
    Pass the SPARQL query to the endpoint. The endpoint is specified in the config file.

    :param sparql: the valid SPARQL text
    :type sparql: str
    :param infer:
    :type infer: bool
    :param same_as:
    :type same_as: bool
    :param limit:
    :type limit: int
    :param offset:
    :type offset: int
    :return:
    :rtype: dict
    """
    loop = asyncio.get_event_loop()
    try:
        session = query_graphdb_endpoint.session_cache[loop]
    except KeyError:
        session = ClientSession(loop=loop)
        query_graphdb_endpoint.session_cache[loop] = session
    args = {
        'query': sparql,
        'infer': 'true' if bool(infer) else 'false',
        'sameAs': 'true' if bool(same_as) else 'false',
        'limit': int(limit),
        'offset': int(offset),
    }
    headers = {
        'Accept': "application/sparql-results+json,*/*;q=0.9",
        'Accept-Encoding': "gzip, deflate",
    }
    resp = await session.request('POST', TRIPLESTORE_CACHE_SPARQL_ENDPOINT, data=args, headers=headers)
    resp_content = await resp.text()
    return loads(resp_content)
query_graphdb_endpoint.session_cache = {}

async def check_type(target_uri, output_featuretype_uri):
    """
    check if resource_uri is of type output_featuretype_uri 
    return boolean 
    :param target_uri:
    :type target_uri: str
    :param output_featuretype_uri:
    :type output_featuretype_uri: str
    :return:
    :rtype: bool
    """
    sparql = """\
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
select * where { 
    BIND(EXISTS{<TARGETURI> rdf:type <TARGETTYPE>} AS ?a)
} 
"""
    sparql = sparql.replace("<TARGETURI>", "<{}>".format(str(target_uri)))
    sparql = sparql.replace("<TARGETTYPE>", "<{}>".format(str(output_featuretype_uri)))
    resp = await query_graphdb_endpoint(sparql)
    results = []
    if 'results' not in resp:
        return locations
    bindings = resp['results']['bindings']
    for b in bindings:
        results.append(b['a']['value'])
    return results[0]  == "true"

async def get_resource(resource_uri):
    """
    :param resource_uri:
    :type resource_uri: str
    :return:
    """
    sparql = """\
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT DISTINCT ?p ?o ?p1 ?o1 ?p2 ?o2
WHERE {
    {
        ?s rdf:subject <URI> ;
           rdf:predicate ?p;
           rdf:object ?o .
        OPTIONAL { FILTER (isBlank(?o))
            {
                ?s2 rdf:subject ?o ;
                rdf:predicate ?p1;
                rdf:object ?o1 .
            }
            UNION
            { ?o ?p1 ?o1 . }
            OPTIONAL { FILTER (isBlank(?o1))
                ?o1 ?p2 ?o2 .
            }
        }
    }
    UNION
    {
        <URI> ?p ?o .
        OPTIONAL { FILTER (isBlank(?o))
            {
                ?s3 rdf:subject ?o ;
                rdf:predicate ?p1;
                rdf:object ?o1 .
            }
            UNION
            { ?o ?p1 ?o1 . }
            OPTIONAL { FILTER (isBlank(?o1))
                ?o1 ?p2 ?o2 .
            }
        }
    }
}
"""
    sparql = sparql.replace("<URI>", "<{}>".format(str(resource_uri)))
    resp = await query_graphdb_endpoint(sparql)
    resp_object = {}
    if 'results' not in resp:
        return resp_object
    bindings = resp['results']['bindings']
    for b in bindings:
        pred = b['p']['value']
        obj = b['o']
        if obj['type'] == "bnode":
            try:
                obj = resp_object[pred]
            except KeyError:
                resp_object[pred] = obj = {}
            pred1 = b['p1']['value']
            obj1 = b['o1']
            if obj1['type'] == "bnode":
                try:
                    obj1 = obj[pred1]
                except KeyError:
                    obj[pred1] = obj1 = {}
                pred2 = b['p2']['value']
                obj2 = b['o2']['value']
                obj1[pred2] = obj2
            else:
                obj1 = obj1['value']
            obj[pred1] = obj1
        else:
            obj = obj['value']
        resp_object[pred] = obj
    return resp_object


async def get_linksets(count=1000, offset=0):
    """
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    :rtype: tuple
    """
    sparql = """\
PREFIX loci: <http://linked.data.gov.au/def/loci#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?l
WHERE {
    {
        ?l a loci:Linkset .
    }
    UNION
    {
        ?c rdfs:subClassOf+ loci:Linkset .
        ?l a ?c .
    }
}
"""
    print(sparql)
    resp = await query_graphdb_endpoint(sparql, limit=count, offset=offset)
    linksets = []
    if 'results' not in resp:
        return linksets
    bindings = resp['results']['bindings']
    for b in bindings:
        linksets.append(b['l']['value'])
    meta = {
        'count': len(linksets),
        'offset': offset,
    }
    print(meta)
    return meta, linksets

async def get_datasets(count=1000, offset=0):
    """
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    :rtype: tuple
    """
    sparql = """\
PREFIX dcat: <http://www.w3.org/ns/dcat#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?d
WHERE {
    {
        ?d a dcat:Dataset .
    }
    UNION
    {
        ?c rdfs:subClassOf+ dcat:Dataset .
        ?d a ?c .
    }
}
"""
    resp = await query_graphdb_endpoint(sparql, limit=count, offset=offset)
    datasets = []
    if 'results' not in resp:
        return datasets
    bindings = resp['results']['bindings']
    for b in bindings:
        datasets.append(b['d']['value'])
    meta = {
        'count': len(datasets),
        'offset': offset,
    }
    return meta, datasets

async def get_locations(count=1000, offset=0):
    """
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    :rtype: tuple
    """
    sparql = """\
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT DISTINCT ?l
WHERE {
    { ?l a geo:Feature }
    UNION
    {
        ?c1 rdfs:subClassOf+ geo:Feature .
        ?l a ?c1 .
    }
    UNION
    {
        ?s1 rdf:subject ?l ;
            rdf:predicate rdf:type ;
            rdf:object geo:Feature .
    }
    UNION
    { ?l a prov:Location }
    UNION
    {
        ?c2 rdfs:subClassOf+ prov:Location .
        ?l a ?c2 .
    }
    UNION
    {
        ?s2 rdf:subject ?l ;
            rdf:predicate rdf:type ;
            rdf:object prov:Location .
    } .
}
"""
    resp = await query_graphdb_endpoint(sparql, limit=count, offset=offset)
    locations = []
    if 'results' not in resp:
        return locations
    bindings = resp['results']['bindings']
    for b in bindings:
        locations.append(b['l']['value'])
    meta = {
        'count': len(locations),
        'offset': offset,
    }
    return meta, locations


async def get_location_is_within(target_uri, count=1000, offset=0):
    """
    :param target_uri:
    :type target_uri: str
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    :rtype: tuple
    """
    sparql = """\
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT DISTINCT ?l
WHERE {
    {
        ?s rdf:subject <URI> ;
           rdf:predicate geo:sfWithin ;
           rdf:object ?l  .
    }
    UNION
    { <URI> geo:sfWithin+ ?l }
}
"""
    sparql = sparql.replace("<URI>", "<{}>".format(str(target_uri)))
    resp = await query_graphdb_endpoint(sparql, limit=count, offset=offset)
    locations = []
    if 'results' not in resp:
        return locations
    bindings = resp['results']['bindings']
    for b in bindings:
        locations.append(b['l']['value'])
    meta = {
        'count': len(locations),
        'offset': offset,
    }
    return meta, locations

async def get_location_contains(target_uri, count=1000, offset=0):
    """
    :param target_uri:
    :type target_uri: str
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    :rtype: tuple
    """
    sparql = """\
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT DISTINCT ?l
WHERE {
    {
        ?s rdf:subject <URI> ;
           rdf:predicate geo:sfContains;
           rdf:object ?l  .
    }
    UNION
    { <URI> geo:sfContains+ ?l }
}
"""
    sparql = sparql.replace("<URI>", "<{}>".format(str(target_uri)))
    resp = await query_graphdb_endpoint(sparql, limit=count, offset=offset)
    locations = []
    if 'results' not in resp:
        return locations
    bindings = resp['results']['bindings']
    for b in bindings:
        locations.append(b['l']['value'])
    meta = {
        'count': len(locations),
        'offset': offset,
    }
    return meta, locations

async def query_build_response_bindings(sparql, count, offset, bindings):
    """
    :param sparql:
    :type sparql: str
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    """
    resp = await query_graphdb_endpoint(sparql, limit=count, offset=offset)
    if 'results' in resp and 'bindings' in resp['results']:
        if len(resp['results']['bindings']) > 0:
            if len(resp['results']['bindings'][0].keys()) > 0:
                bindings.extend(resp['results']['bindings'])

async def get_location_overlaps_crosswalk(from_uri, output_featuretype_uri, include_areas, include_proportion, include_within, include_contains, include_count=1000, offset=0):
    """
    find location overlaps across spatial hierarchies
    :param target_uri:
    :param target_feature_type:
    :type target_uri: str
    :type include_areas: bool
    :type include_proportion: bool
    :type include_within: bool
    :type include_contains: bool
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    """
    # for a from_uri
    # get all contained objects
    # iterate over all responses
    # if a "base unit" e.g a meshblock or a contracted catchment note how much of the from_uri is part of this base unit 
    # find things that overlap with this base unit that are other base units e.g if this is a meshblock find contracted catchments
    # get the amount of intersection with original from_uri by multiplying it by the reversePercentage (i.e amount base units intersect)  
    # find the new base units various parent units and add the forward areas to
    # collate and sum all results for target units
    # calcuate final foward and reverse proportions by finding the proportions of passed over area that is in the target block (reversePercentage)
    # and the proportion of final passed over area as as a proportion of the orignal area (fowardProportion) 
    base_unit_prefix, resource_type_prefix = await get_to_base_unit_and_type_prefix("", from_uri)
    # this is a base unit so continue to base unit logic
    parent_amount = {}
    # cache of withins, base units in other hierarchary may overlap multiple times so don't need to find parents everytime
    # just use cache of parents 
    found_parents = {}
    if not base_unit_prefix in from_uri:
        #This must be a parent unit so get everything contained and find base units
        my_area, all_contained = await get_all_overlaps(from_uri, include_contains=True, include_within=False)
        collated_uri_dict = {} 
        for an_contained in all_contained:
            from_base_uri = an_contained['uri'] 
            if base_unit_prefix is None:
                continue
            if not base_unit_prefix in from_base_uri:
                # isn't actually a base uri but record information 
                parent_amount[from_base_uri] = { "uri" : from_base_uri, "featureArea": my_area, "forwardPercentage" : an_contained["forwardPercentage"], "reversePercentage": an_contained["reversePercentage"], "intersectionArea" : an_contained["intersectionArea"]} 
            # found a base uri do base uri logic 
            percentage_from_uri_in_from_base_uri = float(an_contained["forwardPercentage"]) # This is the amount this base unit takes up of the parent unit
            area_parent = float(my_area) * percentage_from_uri_in_from_base_uri / 100 
            await get_location_overlaps_crosswalk_base_uri(found_parents, parent_amount, area_parent, percentage_from_uri_in_from_base_uri, from_base_uri)
    else:
        my_area = await get_location_overlaps_crosswalk_base_uri(found_parents, parent_amount, None, 100, from_uri)

    parents = parent_amount.values()
    final_parents = []
    for aparent in parents:
        if not output_featuretype_uri is None:
            type_match = await check_type(aparent['uri'], output_featuretype_uri)
            if type_match:
               final_parents.append(aparent)
            else:
               continue
        area_from_uri = float(my_area)
        area_parent = float(aparent["featureArea"])
        area_from_uri_in_parent = aparent["intersectionArea"]
        if include_proportion and not "forwardPercentage" in aparent.keys():
            proportion_area_of_from_uri = area_from_uri_in_parent / area_from_uri 
            if proportion_area_of_from_uri >= 1:
                proportion_area_of_from_uri = 1
            aparent["forwardPercentage"] = str(proportion_area_of_from_uri * 100)
        if not include_proportion and "forwardPercentage" in aparent.keys():
           aparent.pop("fowardProportion", None)
        if include_proportion and not "reversePercentage" in aparent.keys():
            proportion_area_of_parent = area_from_uri_in_parent / area_parent
            if proportion_area_of_parent >= 1:
                proportion_area_of_parent = 1
            aparent["reversePercentage"] = str(proportion_area_of_parent * 100)
        if not include_proportion and "reversePercentage" in aparent.keys():
            aparent.pop("reversePercentage", None)
        if include_areas:
            aparent["intersectionArea"] = str(aparent["intersectionArea"])
        else:
            aparent.pop("intersectionArea", None)
            aparent.pop("featureArea", None)
    meta = {
        'count': len(final_parents),
        'offset': 0,
    }
    if my_area and include_areas:
        meta['featureArea'] = my_area
    return meta, list(final_parents)

async def get_location_overlaps_crosswalk_base_uri(found_parents, parent_amount, area_incoming, percentage_from_uri_in_from_base_uri, from_base_uri):
    """
    find location overlaps across to "to" spatial hierarchies given a base uri in a "from" hierarchy
    """
    my_area, all_overlaps = await get_all_overlaps(from_base_uri, include_contains=True, include_within=True)
    # if there is no area incoming from another higher level object then this is the U shaped query is a L shaped and starts 
    # from a base_uri therefore the area is the area of the base_uri
    if area_incoming is None:
        area_incoming = float(my_area)
    for an_overlap in all_overlaps:
        to_base_uri = an_overlap["uri"]
        base_unit_prefix, resource_type_prefix = await get_to_base_unit_and_type_prefix(from_base_uri, to_base_uri)
        if base_unit_prefix is None:
            continue
        if not base_unit_prefix in to_base_uri:
            continue
        to_feature_area = an_overlap["featureArea"]
        # found an overlapping base unit
        if not to_base_uri in parent_amount.keys(): 
            parent_amount[to_base_uri] = { "uri" : to_base_uri, "intersectionArea" : 0, "featureArea" : to_feature_area, "forwardPercentage" : 0 } 
        percentage_from_base_uri_in_to_base_uri = an_overlap["forwardPercentage"]
        area_from_other_base_uri = (float(percentage_from_base_uri_in_to_base_uri) / 100 * area_incoming) 
        parent_amount[to_base_uri]["intersectionArea"] += area_from_other_base_uri 
        parent_amount[to_base_uri]["forwardPercentage"] = percentage_from_base_uri_in_to_base_uri
        parent_amount[to_base_uri]["reversePercentage"] = an_overlap["reversePercentage"] 
        # find all its parents
        if not to_base_uri in found_parents.keys():
            found_parents[to_base_uri] = await get_all_overlaps(to_base_uri, include_contains=False, include_within=True)
        parent_area, all_within = found_parents[to_base_uri]
        for an_within in all_within:
            within_uri = an_within["uri"]
            if not resource_type_prefix in within_uri:
               continue
            feature_area = an_within["featureArea"]
            # how much of this bigger thing is in the thing in the to hierarchy
            if not resource_type_prefix in within_uri:
                continue
            # this is a parent of the to_base_unit
            if not within_uri in parent_amount.keys(): 
                parent_amount[within_uri] = { "uri" : within_uri, "intersectionArea": 0, "featureArea" : feature_area} 
            parent_amount[within_uri]["intersectionArea"] += area_from_other_base_uri 
    return my_area 


async def get_location_overlaps(target_uri, output_featuretype_uri, include_areas, include_proportion, include_within, include_contains, count=1000, offset=0):
    """
    :param target_uri:
    :type target_uri: str
    :type include_areas: bool
    :type include_proportion: bool
    :type include_within: bool
    :type include_contains: bool
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    """
    overlaps_sparql = """\
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX geox: <http://linked.data.gov.au/def/geox#>
PREFIX qb4st: <http://www.w3.org/ns/qb4st/>
PREFIX epsg: <http://www.opengis.net/def/crs/EPSG/0/>
PREFIX dt: <http://linked.data.gov.au/def/datatype/>
SELECT <SELECTS>
WHERE {
    {
        {
           ?s1 rdf:subject <URI> ;
           rdf:predicate geox:transitiveSfOverlap;
           rdf:object ?o  .
        } UNION {
           ?s2 rdf:subject <URI> ;
           rdf:predicate geo:sfOverlaps;
           rdf:object ?o  .
        }
    }
    UNION
    { <URI> geox:transitiveSfOverlap ?o }
    UNION
    { <URI> geo:sfOverlaps ?o }
    <EXTRAS>
}
GROUP BY ?o
"""
    contains_sparql = """\
    PREFIX geo: <http://www.opengis.net/ont/geosparql#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX geox: <http://linked.data.gov.au/def/geox#>
    PREFIX qb4st: <http://www.w3.org/ns/qb4st/>
    PREFIX epsg: <http://www.opengis.net/def/crs/EPSG/0/>
    PREFIX dt: <http://linked.data.gov.au/def/datatype/>
    SELECT ?c <SELECTS>
    WHERE {
        {  
            ?s2 rdf:subject <URI> ;
            rdf:predicate geo:sfContains;
            rdf:object ?o  .
        }
        UNION
        { <URI> geo:sfContains+ ?o }
        OPTIONAL { FILTER(bound(?o))
            BIND(true as ?c) .
        }
        <EXTRAS>
    }
    GROUP BY ?c ?o
    """
    within_sparql = """\
    PREFIX geo: <http://www.opengis.net/ont/geosparql#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX geox: <http://linked.data.gov.au/def/geox#>
    PREFIX qb4st: <http://www.w3.org/ns/qb4st/>
    PREFIX epsg: <http://www.opengis.net/def/crs/EPSG/0/>
    PREFIX dt: <http://linked.data.gov.au/def/datatype/>
    SELECT ?w <SELECTS>
    WHERE {
        {  
            ?s2 rdf:subject <URI> ;
            rdf:predicate geo:sfWithin;
            rdf:object ?o  .
        }
        UNION
        { <URI> geo:sfWithin+ ?o }
        OPTIONAL { FILTER(bound(?o))
            BIND(true as ?w) .
        }
        <EXTRAS>
    }
    GROUP BY ?w ?o
    """
    use_areas_sparql = include_proportion or include_areas
    use_proportion_sparql = include_proportion

    selects = "?o "
    area_selects = "(MAX(?a1) as ?uarea) (MAX(?a2) as ?oarea) "
    iarea_selects = "(MAX(?a3) as ?iarea) "

    areas_sparql = """\
    OPTIONAL {
        <URI> geox:hasAreaM2 ?ha1 .
        ?ha1 qb4st:crs epsg:3577 .
        ?ha1 dt:value ?a1 .
    }
    OPTIONAL {
        ?o geox:hasAreaM2 ?ha2 .
        ?ha2 qb4st:crs epsg:3577 .
        ?ha2 dt:value ?a2 .
    }
    """
    iarea_sparql = """\
    OPTIONAL {
        { <URI> geo:sfContains ?i }
        UNION 
        {
            ?s3 rdf:subject <URI> ;
                rdf:predicate geo:sfContains ;
                rdf:object ?i 
        } .
        
        { ?o geo:sfContains ?i }
        UNION 
        {
            ?s4 rdf:subject ?o ;
                rdf:predicate geo:sfContains ;
                rdf:object ?i 
        } .
        OPTIONAL {
            ?i geox:hasAreaM2 ?ha3 .
            ?ha3 qb4st:crs epsg:3577 .
            ?ha3 dt:value ?a3 .
        }
    }
    """
    extras = ""
    use_selects = selects
    if use_areas_sparql:
        extras += areas_sparql
        use_selects += area_selects
    if use_proportion_sparql:
        extras += iarea_sparql
        use_selects += iarea_selects
    sparql = overlaps_sparql.replace("<SELECTS>", use_selects)
    sparql = sparql.replace("<EXTRAS>", extras)
    sparql = sparql.replace("<URI>", "<{}>".format(str(target_uri)))
    overlaps = []
    bindings = []
    await query_build_response_bindings(sparql, count, offset, bindings)
    extras = ""
    if include_contains:
        use_selects = selects
        if use_areas_sparql:
            extras = areas_sparql
            use_selects += area_selects
        sparql = contains_sparql.replace("<SELECTS>", use_selects)
        sparql = sparql.replace("<EXTRAS>", extras)
        sparql = sparql.replace("<URI>", "<{}>".format(str(target_uri)))
        await query_build_response_bindings(sparql, count, offset, bindings)
        extras = ""
    if include_within:
        use_selects = selects
        if use_areas_sparql:
            extras = areas_sparql
            use_selects += area_selects
        sparql = within_sparql.replace("<SELECTS>", use_selects)
        sparql = sparql.replace("<EXTRAS>", extras)
        sparql = sparql.replace("<URI>", "<{}>".format(str(target_uri)))
        await query_build_response_bindings(sparql, count, offset, bindings)
    if len(bindings) < 1:
        return {'count': 0, 'offset': offset}, overlaps
    if not include_proportion and not include_areas:
        my_area = False
        for b in bindings:
            overlaps.append(b['o']['value'])
    else:
        d100 = Decimal("100.0")
        try:
            uarea = bindings[0]['uarea']
        except (LookupError, AttributeError):
            raise ReportableAPIError("Source feature does not have a known geometry area."
                                     "Cannot return areas or calculate proportions.")
        my_area = round(Decimal(uarea['value']), 8)
        for b in bindings:
            o_dict = {"uri": b['o']['value']}
            if include_within:
                try:
                    is_w = b['w']
                except (LookupError, AttributeError):
                    is_w = False
                o_dict["isWithin"] = bool(is_w)
            if include_contains:
                try:
                    has_c = b['c']
                except (LookupError, AttributeError):
                    has_c = False
                o_dict["contains"] = bool(has_c)

            overlaps.append(o_dict)
            try:
                oarea = b['oarea']
            except (LookupError, AttributeError):
                continue
            o_area = round(Decimal(oarea['value']), 8)
            if include_areas:
                o_dict['featureArea'] = str(o_area)
            if include_proportion:
                if include_within and is_w:
                    my_proportion = d100
                    other_proportion = (my_area / o_area) * d100
                    i_area = my_area
                elif include_contains and has_c:
                    my_proportion = (o_area / my_area) * d100
                    other_proportion = d100
                    i_area = o_area
                else:
                    try:
                        i_area = Decimal(b['iarea']['value'])
                    except (LookupError, AttributeError):
                        continue
                    my_proportion = (i_area / my_area) * d100
                    other_proportion = (i_area / o_area) * d100
                if include_areas:
                    o_dict['intersectionArea'] = str(round(i_area, 8))
                my_proportion = round(my_proportion, 8)
                other_proportion = round(other_proportion, 8)
                o_dict['forwardPercentage'] = str(my_proportion)
                o_dict['reversePercentage'] = str(other_proportion)

    meta = {
        'count': len(overlaps),
        'offset': offset,
    }
    if my_area and include_areas:
        meta['featureArea'] = str(my_area)
    final_overlaps = overlaps 
    if not output_featuretype_uri is None:
        final_overlaps = []
        for an_overlap in overlaps:
                if isinstance(an_overlap, str):
                    uri_to_check = an_overlap
                else:
                    uri_to_check = an_overlap['uri']
                type_match = await check_type(uri_to_check, output_featuretype_uri)
                if type_match:
                   final_overlaps.append(an_overlap)
    return meta, final_overlaps 


async def get_at_location(lat, lon, loci_type="any", count=1000, offset=0):
    """
    :param lat:
    :type lat: float 
    :param lon:
    :type lon: float 
    :param count:
    :type count: int
    :param offset:
    :type offset: int
    :return:
    """
    if get_at_location.pool is None:
        get_at_location.pool = await asyncpg.create_pool('postgresql://postgres:password@{}:5437/mydb'.format(GEOBASE_ENDPOINT), command_timeout=60, min_size=1, max_size=2)
    conn = await get_at_location.pool.acquire() 
    row = {} 
    results = {} 
    counter = 0
    try:
        if loci_type == 'mb' or loci_type == 'any': 
            row = await conn.fetchrow(
                    'select mb_code_20 from "from" where ST_Intersects(ST_Transform(ST_GeomFromText(\'POINT(\' || $1 || \' \' || $2 || \')\', 4326),3577), "from".geom_3577) order by mb_code_20 limit $3 offset $4', str(lon), str(lat), count, offset)
            if row is not None and len(row) > 0: 
                results["mb"] = ["http://linked.data.gov.au/dataset/asgs2016/meshblock/{}".format(row['mb_code_20'])]
                counter += len(row)
        if loci_type == 'cc' or loci_type == 'any':
            row = await conn.fetchrow(
                    'select hydroid from "to" where ST_Intersects(ST_Transform(ST_GeomFromText(\'POINT(\' || $1 || \' \' || $2 || \')\', 4326),3577), "to".geom_3577) order by hydroid limit $3 offset $4', str(lon), str(lat), count, offset)
            if row is not None and len(row) > 0: 
                results["cc"] = ["http://linked.data.gov.au/dataset/geofabric/contractedcatchment/{}".format(row['hydroid'])]
                counter += len(row)
    finally:
        await get_at_location.pool.release(conn)
    meta = {
        'count': counter,
        'offset': offset,
    }
    return meta, results
get_at_location.pool = None 
       
async def query_es_endpoint(query, limit=10, offset=0):
    """
    Pass the ES query to the endpoint. The endpoint is specified in the config file.

    :param query: the query text
    :type query: str
    :param limit:
    :type limit: int
    :param offset:
    :type offset: int
    :return:
    :rtype: dict
    """
    loop = asyncio.get_event_loop()
    http_ok = [200]
    try:
        session = query_es_endpoint.session_cache[loop]
    except KeyError:
        session = ClientSession(loop=loop)
        query_es_endpoint.session_cache[loop] = session
    args = {
        'q': query
#        'limit': int(limit),
#        'offset': int(offset),
    }
    
    formatted_resp = {
        'ok': False
    }
    try:
        resp = await session.request('GET', ES_ENDPOINT, params=args)
        resp_content = await resp.text()
        if resp.status not in http_ok:
            formatted_resp['errorMessage'] = "Could not connect to the label search engine. Error code {}".format(resp.status)
            return formatted_resp
        formatted_resp = loads(resp_content)
        formatted_resp['ok'] = True
        return formatted_resp
    except ClientConnectorError:
        formatted_resp['errorMessage'] = "Could not connect to the label search engine. Connection error thrown."
        return formatted_resp       
    return formatted_resp
query_es_endpoint.session_cache = {}


async def search_location_by_label(query):
    """
    Query ElasticSearch endpoint and search by label of LOCI locations. 
    The query to ES is in the format of http://localhost:9200/_search?q=NSW

    Returns response back from ES as-is.

    :param query: query string for text matching on label of LOCI locations
    :type query: str
    :return:
    :rtype: dict
    """
    resp = await query_es_endpoint(query)
    
    if ('ok' in resp and resp['ok'] == False):
        return resp
    
    resp_object = {}
    if 'hits' not in resp:        
        return resp_object
    
    resp_object = resp
    return resp_object   

