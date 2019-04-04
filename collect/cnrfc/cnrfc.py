# -*- coding: utf-8 -*-
import datetime as dt
import io
import os
import zipfile
from bs4 import BeautifulSoup
from dateutil import parser
from dotenv import load_dotenv
import pandas as pd
from pytz import timezone
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from collect.utils import clean_fixed_width_headers

UTC = timezone('UTC')
PACIFIC = timezone('America/Los_Angeles')
TODAY = dt.datetime.now().strftime('%Y%m%d')


# load credentials
load_dotenv()

# disable warnings in crontab logs
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_seasonal_trend_tabular(cnrfc_id, water_year):
    """
    adapted from data accessed in py_water_supply_reporter.py
    CNRFC Ensemble Product 7
    """

    url = '?'.join(['http://www.cnrfc.noaa.gov/ensembleProductTabular.php', 
                    'id={}&prodID=7&year={}'.format(cnrfc_id, water_year)])
   
    # retrieve from public CNRFC webpage
    result = requests.get(url).content
    result = BeautifulSoup(result, 'lxml').find('pre').text.replace('#', '')

    # in-memory file buffer
    with io.StringIO(result) as buf:

        # parse fixed-width text-formatted table
        df = pd.read_fwf(buf, 
                         header=[1, 2, 3, 4, 5], 
                         skiprows=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 16], 
                         na_values=['<i>Missing</i>', 'Missing'])
    
    # clean columns
    df.columns = clean_fixed_width_headers(df.columns)

    # clean missing data rows
    df.dropna(subset=['Date (mm/dd/YYYY)'], inplace=True)
    df.drop(df.last_valid_index(), axis=0, inplace=True)

    # parse dates
    df.index = pd.to_datetime(df['Date (mm/dd/YYYY)'])
    df.index.name = 'Date'

    # parse summary from pre-table notes
    notes = result.splitlines()[:10]
    summary = {}
    for line in notes[2:]:
        if bool(line.strip()):
            k, v = line.strip().split(': ')
            summary.update({k: v.strip()})
    
    return {'data': df, 'info': {'url': url,
                                 'type': 'Seasonal Trend Tabular (Apr-Jul)',
                                 'title': notes[0],
                                 'summary': summary,
                                 'units': 'TAF',
                                 'downloaded': dt.datetime.now().strftime('%Y-%m-%d %H:%M')}}


def get_deterministic_forecast(cnrfc_id, truncate_historical=False):
    """
    Adapted from SAFCA portal project
    ---
    reads the url and returns a pandas dataframe from a file or the cnrfc url
    cnrfc_id:  CNRFC station id (5 letter id) (e.g. FOLC1)
    convert CSV data to DataFrame, separating historical from forecast inflow series
    """

    # get forecast file from csv url
    url = 'https://www.cnrfc.noaa.gov/restricted/graphicalRVF_csv.php?id={0}'.format(cnrfc_id)
    basic_auth = requests.auth.HTTPBasicAuth(os.getenv('CNRFC_USER'), os.getenv('CNRFC_PASSWORD'))
    content = requests.get(url, auth=basic_auth).content
    
    # read historical and forecast series from CSV
    with io.BytesIO(content) as csvdata:
        df = pd.read_csv(csvdata, 
                         header=0, 
                         parse_dates=[0],
                         index_col=0,
                         float_precision='high',
                         dtype={'Date/Time (Pacific Time)': str, 
                                'Flow (CFS)': float, 
                                'Trend': str})
        
    # add timezone info
    df.index = [PACIFIC.localize(x) for x in df.index]
    
    # Trend value is null for first historical and first forecast entry; select forecast entry
    first_ordinate = df.where(df['Trend'].isnull()).dropna(subset=['Flow (CFS)']).last_valid_index()

    # deterministic forecast inflow series
    df['forecast'] = df.loc[(df.index >= first_ordinate), 'Flow (CFS)']

    # optional limit for start of historical data (2 days before start of forecast)
    if truncate_historical:
        start = first_ordinate - dt.timedelta(hours=49)
        mask = (df.index > start)
    else:
        mask = True

    # historical inflow series
    df['historical'] = df.loc[(df['forecast'].isnull()) & mask]['Flow (CFS)']

    # forecast metadata
    info = {'url': url,
            'type': 'Deterministic Forecast',
            'first ordinate': first_ordinate.strftime('%Y-%m-%d %H:%M'),
            'units': 'cfs',
            'downloaded': dt.datetime.now().strftime('%Y-%m-%d %H:%M')}

    # additional issuance, plot-type information
    # get issue time of most recent hourly inflow forecast
    # time_issued, next_issue_time, title, plot_type = get_forecast_meta_deterministic(cnrfc_id)
    # info.update({'issuance time': time_issued.strftime('%Y-%m-%d %H:%M'),
    #              'next forecast': next_issue_time.strftime('%Y-%m-%d %H:%M'),
    #              'title': title,
    #              'plot_type': plot_type})

    return {'data': df, 'info': info}


def get_deterministic_forecast_watershed(watershed, date_string, cnrfc_id=None):
    """
    from: https://www.cnrfc.noaa.gov/deterministicHourlyProductCSV.php
    https://www.cnrfc.noaa.gov/csv/2019040318_american_csv_export.zip

    """
    units = 'kcfs'
    date_string = default_date_string(date_string)

    if date_string[-2:] not in ['00', '06', '12', '18']:
        raise ValueError('date_string must be of form %Y%m%d12.')

    # data source
    url = 'https://www.cnrfc.noaa.gov/csv/{0}_{1}_csv_export.zip'.format(date_string, watershed)
    filename = url.split('/')[-1].replace('.zip', '.csv')

    session = requests.Session()
    retries = Retry(total=5,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    content = session.get(url, verify=False).content

    # store content in memory and parse zipped file
    zipdata = io.BytesIO(content)
    zip_ref = zipfile.ZipFile(zipdata)

    # extract CSV from zip object
    csvdata = io.BytesIO(zip_ref.read(filename.replace('.zip', '.csv')))       
    zip_ref.close()

    # parse forecast data from CSV
    df = pd.read_csv(csvdata, 
                     header=0, 
                     skiprows=[1,], 
                     nrows=60, 
                     parse_dates=True, 
                     index_col=0,
                     float_precision='high',
                     dtype={'GMT': str})

    # filter watershed for single forecast point ensemble
    if cnrfc_id is not None:
        columns = [x for x in df.columns if cnrfc_id in x]
    else:
        columns = df.columns
    
    # convert kcfs/day to acre-feet
    df = df[columns] * 1000.0 * ( 3600 * 24 / 43560.0 )
    units = 'cfs'

    # clean up
    zipdata.close()
    csvdata.close()

    # forecast issue time
    time_issued = get_watershed_forecast_issue_time('H', watershed, date_string, deterministic=True)

    return {'data': df, 'info': {'url': url, 
                                 'type': 'Deterministic Forecast', 
                                 'issue_time': time_issued.strftime('%Y-%m-%d %H:%M'),
                                 'watershed': watershed, 
                                 'units': units}}


def get_forecast_meta_deterministic(cnrfc_id, first_ordinate=False):
    """
    Get issuance time from the deterministic inflow forecast page
    """
    
    # request page with CNRFC credentials
    url = 'https://www.cnrfc.noaa.gov/restricted/graphicalRVF_tabular.php?id={0}'.format(cnrfc_id)
    basic_auth = requests.auth.HTTPBasicAuth(os.getenv('CNRFC_USER'), os.getenv('CNRFC_PASSWORD'))
    content = requests.get(url, auth=basic_auth).content

    # parse HTML content
    soup = BeautifulSoup(content, 'lxml')
    title = soup.find_all('font', {'class': 'head'})[0].text

    for td in soup.find_all('td', {'class': 'smallhead'}):
        if 'Issuance Time' in td.text:
            issue_time = parser.parse(td.next_sibling.text)
        if 'Next Issuance' in td.text:
            next_issue_time = parser.parse(td.next_sibling.text)
        if 'Plot Type' in td.text:
            plot_type = td.text.split(':')[1].strip()

    if first_ordinate:
        data = soup.find('pre').text.split('\n')
        forecast_start = parser.parse(data[data.index(u'# FORECAST')+4].strip()[:25])
        return issue_time, next_issue_time, title, plot_type, forecast_start
    
    return issue_time, next_issue_time, title, plot_type


def get_ensemble_forecast(cnrfc_id, duration, acre_feet=False, pdt_convert=False, as_pdt=False):
    """
    from: cnrfc_hourly_forecast_issue_time
          get_station_hourly_ensemble
          get_ensemble_first_forecast_ordinate

    ported from SAFCA Portal project
    possibly CNRFC is labeling GMT when it's actually already in PDT/PST??? - 13Feb2019
    date_string = time_issued.strftime('%Y%m%d_%H%M')
    """

    # default ensemble forecast units    
    units = 'kcfs'

    # validate duration
    if duration[0].upper() == 'H':
        duration = 'hourly'
    elif duration[0].upper() == 'D':
        duration = 'daily'
    else:
        raise ValueError('<duration> must be one of daily, hourly')

    # get issue time of most recent hourly inflow forecast (no support for daily yet)
    date_string = default_date_string(None)
    time_issued = get_watershed_forecast_issue_time(duration, get_watershed(cnrfc_id), date_string)

    # forecast data url
    url = 'https://www.cnrfc.noaa.gov/csv/{0}_hefs_csv_{1}.csv'.format(cnrfc_id, duration)

    # fetch hourly ensemble forecast data
    basic_auth = requests.auth.HTTPBasicAuth(os.getenv('CNRFC_USER'), os.getenv('CNRFC_PASSWORD'))
    session = requests.Session()
    retries = Retry(total=5,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    content = session.get(url, auth=basic_auth, verify=False).text
    
    # read forecast ensemble series from CSV
    with io.StringIO(content) as csvdata:
        df = pd.read_csv(csvdata, 
                         header=0, 
                         skiprows=[1], 
                         parse_dates=[0], 
                         index_col=0, 
                         float_precision='high', 
                         dtype={'GMT': str, cnrfc_id: float})

    # rename columns for ensemble member IDs starting at 1950
    df.columns = [str(x) for x in range(1950, 1950 + len(df.columns))]
    
    # convert kcfs/day to cfs/day
    df = df * 1000.0
    units = 'cfs'

    if acre_feet:
        if duration == 'hourly':
            df = df * ( 3600 / 43560.0 )
        elif duration == 'daily':
            df = df * (24 * 3600 / 43560.0 )
        units = 'acre-feet'

    if pdt_convert:
        df.index = df.index.tz_localize('UTC').tz_convert('America/Los_Angeles')
        df.index.name = 'America/Los_Angeles'
    
    elif as_pdt:
        df.index = [PACIFIC.localize(x) for x in df.index]
        df.index.name = 'America/Los_Angeles'

    return {'data': df, 'info': {'url': url, 
                                 'type': '{0} Ensemble Forecast'.format(duration.title()),
                                 'units': units, 
                                 'issue_time': time_issued.strftime('%Y-%m-%d %H:%M')}}


def get_ensemble_forecast_watershed(watershed, duration, date_string, cnrfc_id=None):
    """
    from: get_watershed_ensemble_issue_time
          get_watershed_ensemble_daily

    download seasonal outlook for the watershed as zipped file, unzip...

    """
    units = 'kcfs'
    date_string = default_date_string(date_string)

    if date_string[-2:] != '12':
        raise ValueError('date_string must be of form %Y%m%d12.')

    # data source
    url = 'http://www.cnrfc.noaa.gov/csv/{0}_{1}_hefs_csv_{2}.zip'.format(date_string, watershed, duration)
    filename = url.split('/')[-1].replace('.zip', '.csv')

    session = requests.Session()
    retries = Retry(total=5,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    content = session.get(url, verify=False).content

    # store content in memory and parse zipped file
    zipdata = io.BytesIO(content)
    zip_ref = zipfile.ZipFile(zipdata)

    # extract CSV from zip object
    csvdata = io.BytesIO(zip_ref.read(filename.replace('.zip', '.csv')))       
    zip_ref.close()

    # get date/time stamp from ensemble download page
    try:
        time_issued = get_watershed_forecast_issue_time(duration, watershed, date_string)
    except:
        time_issued = UTC.localize(dt.datetime.strptime(date_string, '%Y%m%d12')).strftime('%Y-%m-%d 12:00')

    # parse forecast data from CSV
    df = pd.read_csv(csvdata, 
                     header=0, 
                     skiprows=[1,], 
                     nrows=60, 
                     parse_dates=True, 
                     index_col=0,
                     float_precision='high',
                     dtype={'GMT': str})#, 'FOLC1': float})

    # filter watershed for single forecast point ensemble
    if cnrfc_id is not None:
        columns = [x for x in df.columns if cnrfc_id in x]
    else:
        columns = df.columns
    
    # convert kcfs/day to acre-feet
    df = df[columns] * 1000.0 * ( 3600 * 24 / 43560.0 )
    units = 'cfs'

    # clean up
    zipdata.close()
    csvdata.close()

    return {'data': df, 'info': {'url': url, 
                                 'watershed': watershed, 
                                 'issue_time': issue_time,
                                 'units': units, 
                                 'duration': duration}}


def get_watershed_forecast_issue_time(duration, watershed, date_string=None, deterministic=False):
    """
    get "last modified" date/time stamp from CNRFC watershed ensemble product table
    """
    if duration[0].upper() == 'D':
        #" only on the 12"
        date_string = date_string[:-2] + '12'
        url = 'https://www.cnrfc.noaa.gov/ensembleProductCSV.php'
        duration = 'daily'
        file_name = '{0}_{1}_hefs_csv_{2}.zip'
    
    elif duration[0].upper() == 'H':
        url = 'https://www.cnrfc.noaa.gov/ensembleHourlyProductCSV.php'
        duration = 'hourly'
        file_name = '{0}_{1}_hefs_csv_{2}.zip'
    
    if deterministic:
        url = 'https://www.cnrfc.noaa.gov/deterministicHourlyProductCSV.php'
        file_name = '{0}_{1}_csv_export.zip'

    date_string = default_date_string(date_string) 
    content = requests.get(url, verify=False).content
    soup = BeautifulSoup(content, 'lxml')
    
    for td in soup.find_all('td', {'class': 'table-listing-content'}):
        if file_name.format(date_string, watershed, duration) in td.text:
            issue_time = parser.parse(td.next_sibling.text).astimezone(PACIFIC)
            return issue_time


def get_watershed(cnrfc_id):
    """
    get associated hydrologic region for CNRFC forecast location
    """
    if cnrfc_id.upper() in ['KEOO3L', 'BOYO3L', 'IRGC1L', 'BTYO3', 'SCNO3', 'CHSO3', 'WKAO3', 'WMSO3', 'KEOO3', 'BOYO3', 'IRGC1', 'YREC1', 'FTJC1', 'SEIC1', 'SBRC1', 'HAPC1', 'ONSC1', 'CEGC1', 'TRJC1', 'BURC1', 'HYMC1', 'HOOC1']:
        return 'klamath'
    elif cnrfc_id.upper() in ['CREC1', 'FTDC1', 'BLKC1', 'ORIC1', 'MAUC1', 'ARCC1', 'BRGC1', 'DOSC1', 'PLBC1', 'FTSC1', 'LEGC1', 'MRNC1', 'SCOC1', 'FRNC1']:
        return 'NorthCoast'
    elif cnrfc_id.upper() in ['NVRC1', 'LAMC1', 'UKAC1', 'HOPC1', 'CDLC1', 'HEAC1', 'WSDC1', 'GUEC1', 'SHEC1', 'APCC1', 'HOPC1L', 'CDLC1L', 'HEAC1L', 'GUEC1L']:
        return 'RussianNapa'    
    elif cnrfc_id.upper() in ['DLTC1', 'MMCC1', 'MSSC1', 'MSSC1F', 'CNBC1', 'PITC1F', 'SHDC1', 'PLYC1', 'CWAC1', 'COTC1', 'CWCC1', 'WHSC1', 'EDCC1', 'MLMC1', 'TCRC1', 'DCVC1', 'HKCC1', 'BKCC1', 'EPRC1', 'SGEC1', 'BLBC1', 'BDBC1', 'RDBC1', 'TEHC1', 'VWBC1', 'HAMC1', 'ORFC1', 'BTCC1', 'CLSC1', 'CLAC1', 'CLUC1', 'TISC1', 'WLKC1', 'RDBC1L', 'TEHC1L', 'BDBC1L', 'RDBC1L2', 'VWBC1L', 'ORFC1L']:
        return 'UpperSacramento'
    elif cnrfc_id.upper() in ['PLLC1', 'IIFC1', 'ANTC1', 'DVSC1', 'FHDC1', 'SCBC1', 'MFTC1', 'MRMC1', 'NFEC1', 'PLGC1', 'WBGC1', 'ORDC1', 'GYRC1', 'NBBC1', 'HLEC1', 'CFWC1', 'JKRC1', 'BWKC1', 'FOCC1', 'SUAC1', 'SOVC1', 'DMCC1', 'ROLC1I', 'DCSC1', 'JNSC1', 'OURC1', 'LOCC1', 'HCTC1', 'CFWC1O', 'DCWC1', 'NBBC1L', 'OURC1L', 'SUAC1L', 'JNSC1L', 'DCSC1L', 'LOCC1L', 'HLEC1L', 'MRYC1L', 'HLEC1LT', 'MRYC1LT', 'YUBC1L', 'OURC1R', 'JNSC1R', 'NBBC1R']:
        return 'FeatherYuba'
    elif cnrfc_id.upper() in ['MUPC1', 'KCVC1', 'SKPC1', 'CLKC1T', 'HOUC1', 'HCHC1', 'INVC1', 'PCGC1', 'LBEC1']:
        return 'CachePutah'    
    elif cnrfc_id.upper() in ['NFDC1', 'FMDC1', 'FMDC1O', 'BCTC1', 'LNLC1', 'RRGC1', 'HLLC1', 'HLLC1F', 'NMFC1', 'RUFC1', 'MFAC1', 'MFAC1F', 'UNVC1F', 'ICHC1', 'AKYC1', 'AKYC1F', 'CBAC1', 'CBAC1F', 'FOLC1', 'FOLC1F', 'UNVC1', 'SVCC1', 'MFAC1L', 'CBAC1L', 'RBBC1F', 'RBBC1SPL', 'LNLC1F', 'RRGC1L', 'RRGC1F', 'RUFC1L', 'SVCC1F', 'SVCC1L', 'HLLC1L', 'HLLC1SPL']:
        return 'american'
    elif cnrfc_id.upper() in ['SAMC1', 'SACC1', 'VONC1', 'FMWC1', 'DRMC1', 'RCVC1', 'FMWC1L', 'SACC1L', 'SAMC1L', 'NCOC1L']:
        return 'LowerSacramento'
    elif cnrfc_id.upper() in ['LWDC1', 'SNRC1', 'NBYC1', 'NACC1', 'PRBC1', 'RDRC1', 'BSRC1', 'PIIC1', 'TESC1', 'HOSC1', 'PHOC1', 'AROC1', 'BTEC1', 'COYC1', 'ANDC1', 'CYTC1', 'CYEC1', 'CMIC1', 'LEXC1', 'ALRC1', 'GUAC1', 'CADC1', 'ANOC1', 'LVKC1', 'AHOC1', 'CVQC1', 'MPTC1', 'LRZC1', 'SFCC1', 'GUDC1', 'GSJC1']:
        return 'CentralCoast'
    elif cnrfc_id.upper() in ['TWDC1', 'SSQC1', 'GARC1', 'LLYC1', 'CCHC1', 'SLUC1', 'VRVC1', 'CLLC1', 'EFBC1', 'CSKC1', 'PYMC1', 'LKPC1', 'YDRC1', 'SLOC1', 'TEKC1', 'VLKC1', 'MUTC1', 'SMHC1', 'SHRC1', 'TIMC1', 'SREC1', 'SRWC1', 'KNBC1', 'YTLC1', 'MWXC1', 'TSLC1', 'ADOC1', 'SVWC1', 'DKHC1', 'WFMC1', 'MVDC1O', 'MVVC1', 'HAWC1', 'ELPC1', 'SVIC1', 'FSNC1', 'CYBC1', 'HSAC1']:
        return 'SouthernCalifornia'
    elif cnrfc_id.upper() in ['KKVC1', 'SKRC1', 'ISAC1', 'SCSC1', 'KTRC1', 'TMDC1', 'PFTC1', 'DLMC1', 'MLPC1']:
        return 'Tulare'
    elif cnrfc_id.upper() in ['FRAC1', 'HIDC1', 'BHNC1', 'MPAC1', 'OWCC1', 'BNCC1', 'BCKC1', 'MEEC1', 'HPIC1', 'POHC1', 'EXQC1', 'HETC1', 'LNRC1', 'CHVC1F', 'NDPC1', 'NDVC1I', 'NSWC1I', 'AVYC1', 'NMSC1', 'LTDC1', 'DSNC1', 'DCMC1', 'STVC1', 'MDSC1', 'OBBC1', 'RIPC1', 'NWMC1', 'CRDC1', 'PATC1', 'VNSC1']:
        return 'SanJoaquin'
    elif cnrfc_id.upper() in ['CMPC1', 'NHGC1', 'MSGC1', 'FRGC1', 'EDOC1', 'SOSC1', 'MHBC1', 'MCNC1']:
        return 'N_SanJoaquin'
    elif cnrfc_id.upper() in ['SUSC1', 'SCRN2', 'SCWN2', 'WOOC1', 'CEMC1', 'GRDN2', 'STWN2', 'FTCN2', 'WWBC1', 'PSRC1', 'STPC1', 'STPC1F', 'BCAC1', 'BCAC1F', 'FARC1', 'ILAC1', 'LWON2', 'TRRN2', 'VISN2', 'BPRC1', 'SGNC1', 'TAHC1', 'TRCC1', 'DNRC1', 'MTSC1']:
        return 'EastSierra'
    elif cnrfc_id.upper() in ['MBON2', 'MHSN2', 'DVGN2', 'HREN2', 'DIXN2', 'HRCN2', 'PALN2', 'ROCN2', 'HBMN2', 'CMSN2', 'HRIN2', 'LHPN2', 'MARN2', 'MDCN2']:
        return 'Humboldt'
    else:
        raise ValueError('cnrfc_id not recognized.')


def default_date_string(date_string):
    if date_string is None:
        now = dt.datetime.today()
        date_string = now.strftime('%Y%m%d{0}'.format(6 * round(now.hour//6)))
    return date_string


def get_ensemble_first_forecast_ordinate(url):
    """
    return the first date of the forecast (GMT) as datetime object
    """
    df = pd.read_csv(url, 
                     nrows=1, 
                     header=0, 
                     skiprows=[1], 
                     parse_dates=[0], 
                     index_col=0, 
                     float_precision='high',
                     dtype={'GMT': str, 'FOLC1': float})

    return df.index.tolist()[0].to_pydatetime()


if __name__ == '__main__':

    # from pprint import pprint
    RESERVOIRS = {'Folsom': 'FOLC1',
                  'New Bullards Bar': 'NBBC1',
                  'Oroville': 'ORDC1',
                  'Pine Flat': 'PNFC1',
                  'Shasta': 'SHDC1'}

    for watershed in ['klamath', 'NorthCoast', 'RussianNapa', 'UpperSacramento', 'FeatherYuba', 'CachePutah', 'american', 'LowerSacramento', 'CentralCoast', 'SouthernCalifornia', 'Tulare', 'SanJoaquin', 'N_SanJoaquin',  'EastSierra',  'Humboldt']:
        print('*'*88)
        print(watershed)
        print(get_deterministic_forecast_watershed(watershed, '2019040412')['info']['issue_time'])

    # pprint(get_deterministic_forecast('SHDC1', truncate_historical=False)['info'])

    # print(get_ensemble_forecast('SHDC1', 'd')['data'].head())

    # print(get_deterministic_forecast_watershed('american', None)['info'])

    # print(get_ensemble_forecast_watershed('american', 'hourly', None)['data'].head())

    # print(get_seasonal_trend_tabular('SHDC1', 2018)['data'])