# -*- coding: utf-8 -*-

import os
from collections.abc import Sequence
from numbers import Integral
import itertools
import sys
import logging
import inspect

try:
    from osgeo import ogr, osr
    ogr.UseExceptions()
except ImportError:
    pass
from shapely import wkb
from shapely.geometry import (LinearRing, LineString, MultiLineString,
                              MultiPoint, MultiPolygon, Point, Polygon)
import psycopg2

from .geomutils import geom_type_mapping, get_transform_func, unchanged_geom


def setup_logger(name=None, level=logging.INFO, show_pid=False):
    """Setup the logging configuration for a Logger.

    Return a ready-configured logging.Logger instance which will write
    to 'stdout'.


    Keyword arguments:

    name: name of the logging.Logger instance to get. Default is the
    filename where the calling function is defined.
    level: logging level to set to the returned logging.Logger
    instance. Default is logging.INFO.
    show_pid: show the process ID in the log records. Default is
    False.
    """
    if name is None:
        name = os.path.basename(inspect.stack()[1].filename)
    ## Get logger.
    logger = logging.getLogger(name)
    ## Remove existing handlers.
    for handler in logger.handlers:
        logger.removeHandler(handler)
    if level is None:
        logger.disabled = True
        return logger
    ## Set basic logging configuration.
    if show_pid:
        logger.show_pid = True
        pid = f"(PID: {os.getpid()}) "
    else:
        logger.show_pid = False
        pid = ""
    if level <= logging.DEBUG:
        fmt = ("%(asctime)s - %(levelname)s "
               f"- %(name)s {pid}in %(funcName)s (l. %(lineno)d) - "
               "%(message)s")
    else:
        fmt = ("%(asctime)s - %(levelname)s "
               f"- %(name)s {pid}- %(message)s")
    formatter = logging.Formatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.setLevel(level)
    return logger


def update_logger(logger, **kwargs):
    level = kwargs.get("level", logger.getEffectiveLevel())
    if level is None:
        logger.disabled = True
        return
    elif "level" in kwargs.keys():
        logger.disabled = False
    ## Set basic logging configuration.
    if not hasattr(logger, "show_pid"):
        logger.show_pid = False
    show_pid = kwargs.get("show_pid", logger.show_pid)
    if show_pid:
        pid = f"(PID: {os.getpid()}) "
    else:
        pid = ""
    if level <= logging.DEBUG:
        fmt = ("%(asctime)s - %(levelname)s "
               f"- %(name)s {pid}in %(funcName)s (l. %(lineno)d) - "
               "%(message)s")
    else:
        fmt = ("%(asctime)s - %(levelname)s "
               f"- %(name)s {pid}- %(message)s")        
    formatter = logging.Formatter(fmt)
    for handler in logger.handlers:
        handler.setFormatter(formatter)
    logger.setLevel(level)


def fetch_geoms_from_pg(conn=None, host=None, dbname=None, user=None,
                        password=None, port=None, sql_query=None, schema=None,
                        table=None, column=None, aoi=None, aoi_epsg=None,
                        output_epsg=None):
    if conn is None:
        for arg in ("host", "dbname", "user", "password", "port"):
            if locals()[arg] is None:
                raise ValueError(f"Argument {arg!r} must be passed a value "
                                 "different from None!")        
        conn = psycopg2.connect(host=host, dbname=dbname, user=user,
                                password=password, port=port)
    cursor = conn.cursor()
    if sql_query is None:
        for arg in ("schema", "table", "column"):
            if locals()[arg] is None:
                raise ValueError(f"Argument {arg!r} must be passed a value "
                                 "different from None!")
        if aoi is not None or output_epsg is not None:
            cursor.execute(f"SELECT Find_SRID('{schema}', '{table}', "
                           f"'{column}');")
            pg_epsg = int(cursor.fetchone()[0])
        where_filter = f"WHERE {column} IS NOT NULL"
        if aoi is not None:
            if  aoi_epsg is not None and int(aoi_epsg) != pg_epsg:
                transform_aoi = get_transform_func(aoi_epsg, pg_epsg)
                aoi = transform_aoi(aoi)
            spatial_filter = (f" AND ST_Intersects({column}, "
                              f"ST_GeomFromText('{aoi.wkt}', {pg_epsg}));")
        else:
            spatial_filter = ";"
        if output_epsg is not None and int(output_epsg) != pg_epsg:
            column = f"ST_Transform({column}, {output_epsg})"
        sql_query = (f"SELECT ST_AsBinary({column}) FROM {schema}.{table} "
                     f"{where_filter}{spatial_filter}")
    cursor.execute(sql_query)
    for row in cursor:
        yield wkb.loads(row[0].tobytes())
    conn = None    


def _get_layer_epsg(layer):
    lyr_srs = layer.GetSpatialRef()
    if lyr_srs is not None and lyr_srs.AutoIdentifyEPSG() == 0:
        return int(lyr_srs.GetAuthorityCode(None))
    else:
        return None
    

def extract_geoms_from_file(filename, driver_name, layers=None, aoi=None,
                            aoi_epsg=None, attr_filter=None, fids=None):
    logger = setup_logger()
    try:
        from osgeo import ogr
        ogr.UseExceptions()
    except ImportError:
        raise NotImplementedError("You must install GDAL/OGR and its Python "
                                  "bindings to call "
                                  f"{inspect.stack()[0].function!r}!")
    if not os.path.exists(filename):
        raise ValueError(f"The file {filename!r} does not exist!")
    driver = ogr.GetDriverByName(driver_name)
    if driver is None:
        raise ValueError(f"The driver {driver_name!r} is not available or does "
                         "not exist!")
    ds = driver.Open(filename)
    if layers is not None:
        if not isinstance(layers, Sequence) or isinstance(layers, str):
            raise ValueError("'layers' must be passed an iterable of layer "
                             "names/indices!")
    else:
        layers = range(ds.GetLayerCount())
    ## for arg in ("aoi", "aoi_epsg", "attr_filter", "fids"):
    ##     try:
    ##         if locals()[arg] is not None:
    ##             locals()[arg] = dict(locals()[arg])
    ##         else:
    ##             locals()[arg] = dict()
    ##             print(aoi is None)
    ##     except (TypeError, ValueError):
    ##         logger.error(f"The argument {arg_name!r} must passed a mapping "
    ##                      f"of layer names and corresponding {arg_name!r} value.")
    ##         raise
    if aoi is None:
        aoi = dict()
    if aoi_epsg is None:
        aoi_epsg = dict()
    if attr_filter is None:
        attr_filter = dict()
    if fids is None:
        fids = dict()


    ############
    for lyr in layers:
        lyr_obj = ds.GetLayer(lyr)
        lyr_aoi = aoi.get(lyr)
        if lyr_aoi is not None:
            lyr_aoi_epsg = aoi_epsg.get(lyr)
            if lyr_aoi_epsg is not None:
                lyr_aoi_epsg = int(lyr_aoi_epsg)
                lyr_epsg = _get_layer_epsg(lyr_obj)
                if lyr_epsg is not None and lyr_epsg != lyr_aoi_epsg:
                    transform_aoi = get_transform_func(lyr_aoi_epsg, lyr_epsg)
                    lyr_aoi = transform_aoi(lyr_aoi)
            lyr_obj = lyr_obj.SetSpatialFilter(ogr.CreateGeometryFromWkt(lyr_aoi.wkt))
        lyr_attr_filter = attr_filter.get(lyr)
        if lyr_attr_filter is not None:
            lyr_obj = lyr_obj.SetAttributeFilter(lyr_attr_filter)
        lyr_fids = fids.get(lyr)
        if lyr_aoi is None and lyr_attr_filter is None and lyr_fids is not None:
            for fid in lyr_fids:
                feature = lyr_obj.GetFeature(fid)
                geom = feature.GetGeometryRef()
                yield wkb.loads(bytes(geom.ExportToWkb()))
        else:
            for feature in lyr_obj:
                geom = feature.GetGeometryRef()
                yield wkb.loads(bytes(geom.ExportToWkb()))
    ds = None
 

def write_geoms_to_file(geoms_iter, geoms_epsg, filename, driver_name,
                        layer_name, mode="update"):
    logger = setup_logger()
    try:
        from osgeo import ogr, osr
        ogr.UseExceptions()
    except ImportError:
        raise NotImplementedError("You must install GDAL/OGR and its Python "
                                  "bindings to call "
                                  f"{inspect.stack()[0].function!r}!")
    driver = ogr.GetDriverByName(driver_name)
    geoms_epsg = int(geoms_epsg)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(geoms_epsg)
    geoms_iter = iter(geoms_iter)
#    geoms_list = iter(geoms_iter)
#    if not len(set(g.__class__ for g in geoms_list)) == 1:
#        raise ValueError("Cannot process input geometries of different types!")
    first_geom = next(geoms_iter)
    geom_type = geom_type_mapping[first_geom.geom_type]
    geoms_iter = itertools.chain([first_geom], geoms_iter)
    if not mode in ("update", "overwrite"):
        raise ValueError("Wrong value for the 'mode' argument: must be either "
                         "'update' or 'overwrite'!")
    if mode == "update":
        _update_geoms_file(geoms_iter, geom_type, geoms_epsg, srs,
                           filename, driver, layer_name, logger)
    else:
        _write_geoms_file(geoms_iter, geom_type, srs, filename, driver,
                          layer_name, logger)


def _update_geoms_file(geoms_iter, geom_type, geoms_epsg, srs, filename, driver,
                       layer_name, logger):
    ds = driver.Open(filename, 1)
    if ds is None:
        _write_geoms_file(geoms_iter, geom_type, srs, filename, driver,
                          layer_name, logger)
        return
    lyr_obj = ds.GetLayer(layer_name)
    if lyr_obj is None:
        lyr_obj = ds.GetLayer()
    transform_geom = unchanged_geom                
    if lyr_obj is None:
        lyr_obj = ds.CreateLayer(layer_name, srs=srs, geom_type=geom_type)
        lyr_def = lyr_obj.GetLayerDefn()        
    else:
        lyr_def = lyr_obj.GetLayerDefn()
        lyr_epsg = _get_layer_epsg(lyr_obj)
        if lyr_epsg is not None and lyr_epsg !=geoms_epsg:
            logger.info("The spatial reference system of the output file "
                        f"{filename!r}, layer {layer_name!r}, is different "
                        "from that of the input geometry features. The "
                        "geometry features will be reprojected before being "
                        "added to the file.")
            transform_geom = get_transform_func(geoms_epsg, lyr_epsg)
        else:
            logger.info("The spatial reference system of the output file "
                        f"{filename!r}, layer {layer_name!r}, could not be "
                        "found or identified. Input geometry features will be "
                        "added to the file without transformation.")
    for geom in geoms_iter:
        feature = ogr.Feature(lyr_def)
        feature.SetGeometry(ogr.CreateGeometryFromWkt(transform_geom(geom).wkt))
        print(lyr_obj.CreateFeature(feature))
        feature = None
    ds = None


def _write_geoms_file(geoms_iter, geom_type, srs, filename, driver,
                      layer_name, logger):
    if os.path.exists(filename):
        driver.DeleteDataSource(filename)
    ds = driver.CreateDataSource(filename)
    lyr_obj = ds.CreateLayer(layer_name, srs=srs, geom_type=geom_type)
    lyr_def = lyr_obj.GetLayerDefn()
    for geom in geoms_iter:
        feature = ogr.Feature(lyr_def)
        feature.SetGeometry(ogr.CreateGeometryFromWkt(geom.wkt))
        lyr_obj.CreateFeature(feature)
        feature = None
    ## Close the output file.
    ds = None
