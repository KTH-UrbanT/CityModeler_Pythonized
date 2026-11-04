# @Author  :Mohammadhossein Alizadeh
# @Email   : alizad@kth.st@kth.se

import os
from copy import deepcopy
import requests

# The API has three different way of requesting data
class API:
    def __init__(self,BaseURL, UUID):
        self.BaseURL = BaseURL
        self.UUID = UUID
    def Get_Building_by_ID(self):
        APIgetBuilding = []
        for id in self.UUID:
            APIgetBuilding.append(requests.get(os.path.join(self.BaseURL, f'buildings/{id}/')).json())
        APIgetBuilding = [EPC[-1] for EPC in APIgetBuilding]
        return APIgetBuilding

    def Get_Building_by_Single_Filtere(self, filtre):
        return requests.get(f'https://api.opendesignengine.org/v1/buildings?{filtre}')
    def Get_Building_by_Multiple_Filtere(self, filtre):
        return requests.get(f'https://api.opendesignengine.org/v1/buildings?{filtre}')

def GenCore_API(requestedData, CM_Template):
    """Return a template collection populated with one feature per API record.

    Match property names case-insensitively, then try the API's ``epc_``
    prefix and known identifier aliases. Exact names take precedence.
    Absent properties retain template defaults; explicit nulls replace them.
    Geometry is retained from the template: EPC records contain no footprint.
    Neither input is modified. Calculated ``_calc`` fields are not substituted
    for original EPC fields because they may represent different quantities.
    """
    if not isinstance(CM_Template, dict) or CM_Template.get('type') != 'FeatureCollection':
        raise ValueError('CM_Template must be a GeoJSON FeatureCollection.')
    features = CM_Template.get('features')
    if not isinstance(features, list) or not features:
        raise ValueError('CM_Template must contain a template feature.')
    prototype = features[0]
    if not isinstance(prototype, dict) or not isinstance(prototype.get('properties'), dict):
        raise ValueError('The template feature must contain a properties dictionary.')
    if isinstance(requestedData, dict):
        requestedData = [requestedData]
    if not isinstance(requestedData, list):
        raise TypeError('requestedData must be a building dictionary or a list of dictionaries.')

    aliases = {'fnr': '01a_fnr', 'deso': '50a_deso'}
    result = deepcopy(CM_Template)
    result['features'] = []
    for index, record in enumerate(requestedData):
        if not isinstance(record, dict):
            raise TypeError(f'Building record {index} must be a dictionary.')
        values = {key.casefold(): value for key, value in record.items()}
        feature = deepcopy(prototype)
        for name in feature['properties']:
            normalized = name.casefold()
            candidates = [normalized, f'epc_{normalized}']
            if normalized in aliases:
                candidates.append(aliases[normalized])
            for candidate in candidates:
                if candidate in values:
                    feature['properties'][name] = deepcopy(values[candidate])
                    break
        result['features'].append(feature)
    return result
