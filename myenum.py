#!/usr/bin/env python3
"""
Custom Enumeration
"""


from enum import Enum

class MyEnum(Enum): # pylint: disable-msg=R0903
    """Custom enum base class"""
        
    @classmethod
    def get_all_values(self):
        result = []
        for e in self.__members__:
            result.append(self.__members__[e])
        return result
