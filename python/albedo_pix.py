#!/usr/bin/env python

# Python version of albedo2.pro
# NSS 23/11/12
# Lewis 11 June 2013: major modifications

'''Python code to scan over a set of MODIS albedo product files and calculate a weighted mean and measure of variation. Stage 1.'''

__author__      = "P.Lewis, NSS"
__copyright__   = "Copyright 2013, UCL"
__license__ = "GPL"
__version__ = "1.0.7"
__maintainer__ = "P. Lewis"
__email__ = "p.lewis@ucl.ac.uk"
__status__ = "Production"

import numpy as np
import sys,os,datetime,math,ast,glob,resource
from pyhdf import SD
from optparse import OptionParser
#the two imports below are needed if you want to save the output in ENVI format, but they conflict with the netcdf code, so commented out.
#from osgeo import gdal
#import osgeo.gdalconst as gdc
from subprocess import check_call
import netCDF4 as nc
import logging
from scipy import stats
import cPickle as pickle
import zlib
import pp
import numpy.ma as ma

def insensitive_glob(pattern):
    """ From: http://stackoverflow.com/questions/8151300/ignore-case-in-glob-on-linux

    form a case insensitive glob version of a filename (or other) pattern

    Parameters
    ----------
    pattern : a string (filename)
          String to process

    Returns
    -------
    npattern : a string (filename) that is case insensitive

    Examples
    --------

    insenitive = insensitive_glob('/data/fbloggs/*/*.hdf')

    >>> insensitive_glob('/data/*/plewis/h25v06/DATA/*.HDF')[0]
    '/data/geospatial_11/plewis/h25v06/data/MCD43A1.A2001001.h25v06.005.2006360202416.hdf'

    """
    def either(c):
        return '[%s%s]'%(c.lower(),c.upper()) if c.isalpha() else c
    return glob.glob(''.join(map(either,pattern)))

class dummy():
    def __init__(self):
      self.info = self.error = self.warning = None

class prep_modis():
    """  
    Prepare and interpret MODIS MCD43 data

    Initialise as:

    ::

        al = prep_modis(inputs)


    where ``inputs`` should contain (e.g. ``inputs.clean = True``):

        ``doyList`` : list of strings identifying which days to process
                      e.g. ['001','009'] or set to None to select all
                      available
        ``logfile`` : log file name (string)

        ``logdir`` : directory for log file (string)

        ``srcdir`` : Source (MODIS MCD43) data directory

        ``tile``   : MODIS tile ID

        ``backupscale`` : Array defining the scale to map MODIS QA flags to, e.g. 0.6

        ``opdir`` : Output directory

        ``shrink`` : spatial shrink factor (integer) 

        ``sdim`` : image subsection: default [-1,-1,-1,-1] or [l0,nl,s0,ns]

        ``bands`` : list of bands to process. e.g. [7,8,9]

        ``years`` : list of years to process e.g. 
                [2000,2001,2002,2003,2004,2005,2006,2007,2008,2009,2010,2011,2012,2013]

        ``version`` : MODIS collection number (as string). e.g. 005

        ``product`` : product name e.g. mcd43a (case insensitive)

        ``snow`` :  output snow data

        ``no_snow`` :  output no_snow data


    """    
    def __init__(self,inputs=None):
        """
        Class constructor


        Parameters
        ----------
        inputs : structure (e.g. from parser) containing settings

        """

        #constructor
        self.default_settings(inputs)
 
        self.a1Files = []
        self.a2Files = []

        # set up logging and opdir
        self.logging = dummy()
        self.logging.info = self.logging.error = self.logging.warning = self.no_log
        self.d = ''
        if self.logfile:
          self.set_logging(self.logdir,self.logfile)
      
        if not os.path.exists(self.opdir):
          os.makedirs(self.opdir)
        # return a list of valid hdf files
        for doy in self.doyList or np.arange(1,366,8):
          a1,a2 = self.getValidFiles(self.years,doy)
          self.a1Files.extend(a1)
          self.a2Files.extend(a2)

    def no_log(self,msg,extra=''):
        '''
        Default logging - put to stderr
        '''
        import sys
        sys.stdout.write('%s: %s\n'%(extra,msg))

    def default_settings(self,inputs):
        self.ip = inputs
        if inputs == None:
          self.doyList = None
          self.logfile = None
          self.logdir  = 'logs'
          self.years   = np.arange(2000,2100)
          self.product = 'MCD43A'
          self.tile    = 'h18v03'
          self.version = '005'
          self.srcdir  = '.'
          self.bands   = np.arange(7)
          self.shrink  = 1
          self.opdir   = 'results'
          self.sdim    = [-1,-1,-1,-1]
          self.backupscale = 0.61803398875 # ^ band_quality
          self.snow    = True 
          self.no_snow = True
          return

        try:
          self.doyList = self.ip.doyList 
          self.logfile = self.ip.logfile
          self.logdir  = self.ip.logdir
          self.years   = self.ip.years
          self.product = self.ip.product
          self.tile    = self.ip.tile
          self.version = self.ip.version
          self.srcdir  = self.ip.srcdir
          self.bands   = self.ip.bands
          self.shrink  = self.ip.shrink
          self.opdir   = self.ip.opdir
          self.sdim    = self.ip.sdim
          self.backupscale = self.ip.backupscale
          self.snow    = self.ip.snow
          self.no_snow = self.ip.no_snow  
        except:
          self.doyList = self.ip['doyList'] 
          self.logfile = self.ip['logfile']
          self.logdir  = self.ip['logdir']
          self.years   = self.ip['years']
          self.product = self.ip['product']
          self.tile    = self.ip['tile']
          self.version = self.ip['version']
          self.srcdir  = self.ip['srcdir']
          self.bands   = self.ip['bands']
          self.shrink  = self.ip['shrink']
          self.opdir   = self.ip['opdir']
          self.sdim    = self.ip['sdim']
          self.backupscale = self.ip['backupscale']
          self.snow    = self.ip['snow']
          self.no_snow = self.ip['no_snow']

    def translate(self,filename,QaFile,refl=None,bands=None,data=None):
      '''
      We want the processing to be per waveband
      for all years for stage 1

      So we have to store nyears of weight etc (data)
      that we need for processing, and refl[nyears]
      '''
      b = bands or self.bands
      # loop over bands
      data = self.getModisAlbedo(filename,QaFile,\
                snow=self.snow,no_snow=self.no_snow,dataw=data,\
		bands=np.array([b]),sdim=self.sdim,backupscale=self.backupscale)
      if data['error']:
        return None
      data['year'] = filename.split('/')[-1].split('.')[1][1:5]
      data['doy'] = filename.split('/')[-1].split('.')[1][5:]
      data['data'] = (1000*data['data']).astype(np.uint16)
      data['weight'] = (1000*data['weight']).astype(np.uint16)
      return data

    def testing(self):
      # organise the data by doy for all years
      # we need to get the data ordered so that year varies first
      # then doy then anything else
      iofile = self.opdir + '/'+ self.product+'.'+self.tile+'.'+data['doy']+'_base.pkl'
      # and by band
      fp = [None]*len(self.bands)
      files = [None]*len(self.bands)
      for i,b in enumerate(self.bands):
        iofiles = iofile.replace('base.pkl','band_%08d.bin'%b)
        if iofiles == files[i]:
          self.logging.info('appending to %s ...'%iofiles,extra=self.d)
        else:
          if fp[i] != None:
            fp[i].close()
          self.logging.info('writing to %s ...'%iofiles,extra=self.d)
          fp[i] = open(iofiles, 'wb')
        np.save(fp[i], data['data'][:,i])
      del data['data']
      self.logging.info('writing to %s ...'%iofile,extra=self.d) 
      fp = open(iofile, 'wb+')
      pickle.dump(data, fp)
      return True

    def set_logging(self,logdir,logfile):
        # logging
        self.logging = logging
        if os.path.exists(logdir) == 0:
          os.makedirs(logdir)
        try:
          from socket import gethostname, gethostbyname
          self.clientip = gethostbyname(gethostname()) 
        except:
          self.clientip = ''
        try:
          import getpass
          self.user = getpass.getuser()
        except:
          self.user = ''
        self.log = logdir + '/' + logfile
        self.logging.basicConfig(filename=self.log,\
                     filemode='w+',level=self.logging.DEBUG,\
                     format='%(asctime)-15s %(clientip)s %(user)-8s %(message)s')
        self.d = {'clientip': self.clientip, 'user': self.user}
        self.logging.info("__version__ %s"%__version__,extra=self.d)
        print "logging to %s"%self.log
        # set an id for files
        try:
          import uuid
          self.id = unicode(uuid.uuid4())
        except:
          self.id = ""
        # create error file directory
        self.errFiles = './logs'
        d = os.path.dirname(self.errFiles)
        if not os.path.exists(d):
          os.makedirs(d)

    def getDates(self,list1):
        """
        Process a list of date/year entries into a list of days of year (doyList) 
        and years (yearlist)
        
        Parameters
        ----------

        list1 : list of MODIS hdf file names from which we will extract the date information
        
        Returns
        -------

        doyList :  unique, sorted list of DOY 
        yearlist : unique, sorted list of years

        """
        doyList=[]
        yearList=[]
        
        for file in list1:
            fileBaseName = os.path.basename(file)
            year=fileBaseName[9:13]
            doy=fileBaseName[13:16]
            if doyList.count(doy) == 0:
                doyList.append(doy)
            if yearList.count(year) == 0:
                yearList.append(year)

        yearList = np.sort(np.unique(yearList))
        doyList = np.sort(np.unique(doyList))
        self.logging.info(str(yearList),extra=self.d)
        self.logging.info(str(doyList),extra=self.d)

        return doyList, yearList

    def idSamples(self,nb):
        """
        Return a string array describing the data storage for the variance/covariance structures

        Parameters
        ----------

        nb  : long
              number of bands

        Returns
        -------

        retval : string array 
                 containing output band text descriptions


        """
        params = ['F0','F1','F2']
        this= []
        for i in range(nb):
            for j in range(3):
              this.append('MEAN: BAND ' + str(i) + ' PARAMETER ' + params[j])
              this.append('SD: BAND ' + str(i) + ' PARAMETER ' + params[j])

        return this

    def getValidFiles(self,yearList,doy):

        """       
        Return a list of files that can be processed 


        Parameters
        ----------
        
        yearlist  : string array  
                    years to process

        doy       : string       
                    which day to process 


        Returns
        -------

        dictionary : A1FILES,A2FILES

        where:

        A1FILES  : data files
        A2FILES  : QA files

        """
        product = self.product
        tile = self.tile
        version = self.version
        srcdir = self.srcdir
        doy = '%03d'%int(doy)
        a1Files = []
        a2Files = []
        for i,year in enumerate(yearList):
                year = str(yearList[i])
                # look in e.g. srcdir + '/MCD43A2/2000/h18v03/*/*MCD432.A2000000.h18v03.005.*.hdf'
                a2 = insensitive_glob(srcdir+'/%s2/%s/%s/*/'%(product,year,tile)+\
                         '*'+product+'2.A'+year+str(doy)+'.'+tile+'.'+version+'.*.hdf')
                a1 = insensitive_glob(srcdir+'/%s1/%s/%s/*/'%(product,year,tile)+\
                         '*'+product+'1.A'+year+str(doy)+'.'+tile+'.'+version+'.*.hdf')
 
                # look in eg srcdir + '/*MCD43A1.A2000000.h18v03.005.*.hdf'
                if len(a1) == 0 or len(a2) == 0: 
                  a2 = insensitive_glob(srcdir+'/'+'*'+product+'2.A'+year+str(doy)+'.'+tile+'.'+version+'.*.hdf')
                  a1 = insensitive_glob(srcdir+'/'+'*'+product+'1.A'+year+str(doy)+'.'+tile+'.'+version+'.*.hdf')
              
                # look in eg '/*MCD43A2/2000/*.A2000000.h18v03.*hdf'
                if len(a2) == 0 or len(a1) == 0:
                  a2.extend(insensitive_glob(self.srcdir+'/'+'*'+self.product+'2/%s/*.A%s%s.%s.*hdf'%(year,year,str(doy),self.tile)))
                  a1.extend(insensitive_glob(self.srcdir+'/'+'*'+self.product+'1/%s/*.A%s%s.%s.*hdf'%(year,year,str(doy),self.tile)))

                if len(a1) == 0 or len(a2) == 0 :
                    self.logging.info( '========',extra=self.d)
                    self.logging.info( 'inconsistent or missing data for doy \
                              %s year %s tile %s version %s'%(str(doy),str(year),tile,version),extra=self.d)
                    self.logging.info( 'A1: %s'%a1,extra=self.d)
                    self.logging.info( 'A2: %s'%a2,extra=self.d)
                    self.logging.info( '========',extra=self.d)
                else:
                    a1Files.append(a1[0])
                    a2Files.append(a2[0])
                    self.logging.info( a1[0],extra=self.d)
       
        return a1Files,a2Files


    def sortDims(self,ip,op):
        """
        Change the dimensions of the dataset if you want to process a sub-image
        

        Parameters
        ----------
        
        ip  :  Long array[4] 
               containing [s0, ns,l0,nl]

        op  :  Long array[4]
               original data dimensions [s0,send,l0,lend]


        where send, lend are the end sample and line numbers required
        
        Returns
        -------

        op  :  Long array[4]
               DIMS format containing [s0,ns,l0,nl]

        where send, lend are the end sample and line numbers required

        """
        s0 = op[0]
        ns = op[1] - op[0]
        l0 = op[2]
        nl = op[3] - op[2]
        s0 = max(ip[0],s0)

        if ip[1] != -1:
            ns = ip[1]
        if ip[3] != -1:
            nl = ip[3]

        l0 = max(ip[2],l0)
        s0 = min(max(0,s0),op[1])
        l0 = min(max(0,l0),op[3])

        ns = max(min(ns,op[1] - op[0] - s0),1)
        nl = max(min(nl,op[3] - op[2] - l0),1)

        op = [int(s0), int(ns), int(l0), int(nl)]

        return op

    def getModisAlbedo(self,fileName,QaFile,bands=[0,1,2,3,4,5,6],\
                        snow=False,no_snow=True,dataw=None,\
                        sdim=[-1,-1,-1,-1],backupscale=0.61803398875):
        """
        Extract data from MODIS data file (C5)

        Parameters
        ----------

        fileName    : MCD43 data file (A1)
                      The HDF filename containing the required data (e.g. MCD43A1.A2008001.h19v08.005.2008020042141.hdf)
        QaFile      : MCD43 QA file (A2)  
                      The HDF filename for the associated QA datafile(e.g. MCD43A2.A2008001.h19v08.005.2008020042141.hdf)

        Optional:

        snow        : process snow pixels
        no_snow     : process no_snow pixels
        bands       : array e.g. [0,1,2,3,4,5,6]
        sdim        : array [-1,-1,-1,-1] used to ectract subset / subsample. 
                      The format is [s0,ns,l0,nl]
        backupscale : translation quantity for QA flags
                      e.g. 0.61803398875
 
        Returns
        -------

        dictionary : containing

        weight    : weight[ns,nl]
        data:     : data[nb,ns,nl,3]
        mask:     : mask[ns,nl]                 : True for good data
        snow_mask : snow_mask[ns,nl]    : True for snow
        land      : land[ns,nl]         : 1 for land and only land

        error    : bool              
        nb       : long    : number of bands
        ns       : long    : number of samples
        nl       : long    : number of lines

        Notes for land
        -----
        0  :    Shallow ocean
        1  :   Land (Nothing else but land)
        2  :   Ocean and lake shorelines
        3  :  Shallow inland water
        4  :  Ephemeral water
        5  :  Deep inland water
        6  :  Moderate or continental ocean
        7 :   Deep ocean 
         
        see https://lpdaac.usgs.gov/products/modis_products_table/mcd43a2
                                    
        """
        duff = long(32767)
        oneScale = 1000
        scale = 1.0/oneScale
        nBands = len(bands)
        err=0
        try:
          if dataw == None:
            try:
              self.logging.info( '...reading qa... %s'%QaFile,extra=self.d)
            except:
              pass
            # open the QA file
            hdf = SD.SD(QaFile)
            sds_1 = hdf.select(0)
            nl,ns = sds_1.dimensions().values()
            fileDims = [0,ns,0,nl]
            #take a subset if input sdims values require it
            s0,ns,l0,nl = self.sortDims(sdim,fileDims)
  
            # BRDF_Albedo_Quality: 255 is a Fill
            goodData = np.array(sds_1.get(start=[s0,l0],count=[ns,nl])) != 255
  
            # Snow_BRDF_Albedo 
            sds_2 = hdf.select(1)
            QA = np.array(sds_2.get(start=[s0,l0],count=[ns,nl]))
            # snow mask is True for snow and False for no snow
            goodData = goodData & (QA!=255)
            snow_mask    = QA==1
            no_snow_mask = QA==0
            #  BRDF_Albedo_Ancillary
            #  pull land / sea etc mask 
            sds_3 = hdf.select(2)
            QA = np.array(sds_3.get(start=[s0,l0],count=[ns,nl]))
            # land / water is bits 4-7
            land = (( 0b11110000 & QA ) >> 4).astype(np.uint8)
            # dont want deep ocean
            goodData = goodData & (land != 7)

            #  BRDF_Albedo_Band_Quality 
            sds_4 = hdf.select(3)
            QA = np.array(sds_4.get(start=[s0,l0],count=[ns,nl]))
            band_quality = QA & 0b1111
            QA = QA >> 4
            goodData = goodData & (band_quality < 4)
            #this loop might not be needed ...
            #might get away with just teh first band ...
            #for k in range(1,1):
            band_quality2 = QA & 0b1111
            goodData = goodData & (band_quality2 < 4)
            QA = QA >> 4
            # take the max
            w = band_quality2>band_quality
            band_quality[w] = band_quality2[w]
            hdf.end()
          else:
            s0,ns,l0,nl = dataw['limits']
            mask = QA = goodData = dataw['mask']
            land = dataw['land']
            weight = dataw['weight']
            snow_mask = dataw['snow_mask']

          self.logging.info( 'reading data... %s'%fileName,extra=self.d)

          # open the data file 

          hdf = SD.SD(fileName)
          # allocate array for all bands
          data = np.zeros((3, nBands) + QA.shape)
          # loop over bands 
          for i in range(nBands):
            self.logging.info( '  ... band %d'%int(bands[i]),extra=self.d)
            # Lewis: ensure this is an int
            sds = hdf.select(int(bands[i]))
            ithis = np.array(sds.get(start=[s0,l0,0],count=[ns,nl,3]))
            #filter out duff values
            for j in range(3):
              goodData = goodData & (ithis[:,:,j] != duff)
              data[j,i] = scale * ithis[:,:,j]
          hdf.end()

          #self.logging.info( 'done ...',extra=self.d)
          """     
          sort the QA info to assign a weight (specified by backupscale)
          for 0 : best quality, full inversion (WoDs, RMSE majority good)
          1 : good quality, full inversion 
          2 : Magnitude inversion (numobs >=7) 
          3 : Magnitude inversion (numobs >=3&<7) 
          where the QA is determined as the maximum (ie poorest quality) over the wavebands  
          """

          if dataw == None:
            # snow type filtering
            if snow and no_snow:
              pass
            elif snow:
              goodData = goodData & snow_mask
            elif no_snow:
              goodData = goodData & no_snow_mask

            weight = backupscale ** band_quality
  
            self.logging.info( ' ...sorting mask...',extra=self.d)
            mask = goodData
            land      = land * mask
            weight    = weight * mask
            snow_mask = snow_mask * mask
            no_snow_mask = no_snow_mask * mask
            # NB this changes the data set shape around
            # so its data[0-3,nb,:,:]
          data = data * mask

          retval = {'error':False,'ns':ns,'nl':nl,'nb':nBands,\
                        'land':land,'weight':weight,\
                        'limits':(s0,ns,l0,nl),'no_snow_mask':no_snow_mask,\
                        'data':data,'mask':goodData,'snow_mask':snow_mask}
          self.logging.info('done',extra=self.d)
        except:
          retval = {'error':True}
        return retval



    def allocateData(self,nb,ns,nl,nsets,flag):
        """

        Allocate data for calculating image statistics

        Parameters
        ----------
        
        ns  : long 
              number of samples
        nl  : long 
              number of lines
        nb  : long 
              number of bands
        nsets : long
              number of data sets      
 
        Returns
        ------- 

        Dictionary containing:
        
        sum  : sum 
               (weighted by w) for band of nb, kernel of 3, sample of ns, line of nl
        sum2 : sum^2 
               (weighted by w^2) for each combination of band/kernel, sample of ns, line of nl
        n    : sum of weight 
               for sample of ns, line of nl
        nSamples : number of samples 
                   per pixel

        """
        # allocate the arrays for mean and variance
        if (flag == 0):
            
            data = np.zeros((nsets,nb,ns,nl,3),dtype=np.float32)           
            n = np.zeros((nsets,nb,ns,nl,3),dtype=np.float32)
            nSamples = np.zeros((nsets,nb,ns,nl),dtype=np.int)
            self.logging.info( 'numb of bands %d, numb of samples %d, numb of lines %d'%(nb, ns, nl),extra=self.d)
 
        else:
            data = -1
            n = -1
            nSamples = -1

        return dict(n=n, data=data, nSamples=nSamples)


    def incrementSamples(self,sumData,samples,isSnow,index):
        """
        Increment information in the sumdata dictionary with data from a MODIS image in samples dictionary

        The data here are in samples (a dictionary).

        The snow mask is in data['isSnow'] != -1
        i.e. this is set to -1 for 'no data'
        It is set to 1 if a pixel is 'snow' and 0 if snow free

        Parameters
        ----------

        sumData : dictionary
                  Containing ``sum``, ``sum2``, ``n`` and ``nSamples`` that we wish to accumulate into
        samples : dictionary
                  Containing ``sum``, ``sum2``, ``n`` and ``nSamples`` that are the values to be added to sumData
        isSnow : integer
                 Code for snow processing type. The flag isSnow is used to determine the type of coverage:
          0 : no snow only
          1 : snow only
          2 : snow and no snow together

         index : integer
                 dataset index

        Returns
        --------

        sumData : dictionary
                  With ``data``, ``n`` and ``nSamples`` in the arrays

        """
        ns = samples['ns']
        nl = samples['nl']
        nb = samples['nb']

       
        # generate some negative masks 
        if (isSnow == 0):
            # no snow only
            w = np.where(samples['isSnow'] != 0)
        elif (isSnow == 1):
            # snow only
            w = np.where(samples['isSnow'] != 1)
        else:
            w = np.where(samples['isSnow'] == -1)

        # so w is a mask of where we *dont* have data (that we want)
        samplesN = samples['N'].copy()
        samplesN[w] = 0

        # sum f0 over all bands
        f0sum = samples['data'][:,:,:,0].sum(axis=0)
        # find where == 0  as f0 == 0 is likely dodgy     
        w = np.where((f0sum<=0) & (samplesN>0))
        if len(w)>0 and len(w[0])>0:
          self.logging.info('N %.2f'%samplesN.sum(),extra=self.d)
          self.logging.info("deleting %d samples that are zero"%len(w[0]),extra=self.d)
          samplesN[w] = 0
          self.ogging.info('N %.2f'%samplesN.sum(),extra=self.d)
        else:
          self.logging.info('N %.2f'%samplesN.sum(),extra=self.d)
        # save some time on duffers
        if samplesN.sum() == 0:
          self.logging.info('No samples here ...',extra=self.d)
          return sumData

        weight = np.zeros((nb,ns,nl,3),dtype=np.float32)

        for i in range(nb):
            for j in range(3):
                weight[i,:,:,j] = samplesN

        # shrink the data
        sweight = np.zeros((nb,ns/self.shrink,nl/self.shrink,3),dtype=float)
        sdata = np.zeros((nb,ns/self.shrink,nl/self.shrink,3),dtype=float)

        # so sweightdata is the observations multiplied by the weight
        sweightdata = samples['data']*weight
        for i in xrange(nb):
          for j in xrange(3):
            sweight[i,:,:,j] = self.shrunk(weight[i,:,:,j],ns,nl,self.shrink)
            sdata[i,:,:,j] = self.shrunk(sweightdata[i,:,:,j],ns,nl,self.shrink)
            ww = np.where(sweight[i,:,:,j]>0)
            sdata[i,:,:,j][ww] /= sweight[i,:,:,j][ww]
        # now sdata is re-normalised so its just the data again

        # store the data
        sumData['n'][index,...] = sweight
        sumData['nSamples'][index,...] = (sweight[:,:,:,0] > 0).astype(int)
        sumData['data'][index,...] = sdata

        return sumData



    def processAlbedo(self,yearList,doy,sdmins):
        """
         For some given doy, read and process MODIS kernel data 

        Parameters
        ----------

         yearlist  :  string array
                      candidate years to process
         doy       :  long 
                      day of year to process
         SDIMS     :  long array[4]
                      [s0,ns,l0,ns] or [-1,-1,-1,-1] for full dataset
         

         Returns
         -------

         processed              : boolean
                                 True if data read ok otherwise False
         totalSnow              :  long
                                   total number of snow samples
         sumdataNoSnow          :  sumdata-type dict 
                                   for no snow information
         sumdataSnow            :  sumdata-type dict
                                   for snow information
         sumdataWithSnow        : sumdata-type dict 
                                  for snow and no-snow information
         nb                     : long  
                                   number of bands
         ns                     : long
                                   number of samples
         nl                     : long
                                   number of lines
         tile                   : string
                                  tile name
         version                : string
                                   version name (005)
         doy                    : string
                                   doy string to process e.g. 001
         land                   : float[ns,nl]
                                  land codes 
        
         Where:
 
          land category (15 = do not process)
                                0          Shallow ocean
                                1         Land (Nothing else but land)
                                2         Ocean and lake shorelines
                                3         Shallow inland water
                                4         Ephemeral water
                                5         Deep inland water
                                6         Moderate or continental ocean
                                7         Deep ocean
         see https://lpdaac.usgs.gov/lpdaac/products/modis_products_table/brdf_albedo_quality/16_day_l3_global_500m/v5/combined

         See Also
         ---------
         self.increment_samples()
                       

        """
        self.a1Files = None
        self.a2Files = None
        a1Files, a2Files = self.getValidFiles(yearList,doy[0])

        self.logging.info('doy %s'%str(doy[0]),extra=self.d)
        for i in xrange(len(a1Files)):
           self.logging.info('  %d %s %s'%(i,str(a1Files[i]),str(a2Files[i])),extra=self.d)

        foundOne = False
        thisOne = 0
        # try to file at least one file that works ...
        while not foundOne: 
          thisData = self.getModisAlbedo(a1Files[thisOne],a2Files[thisOne],\
                          sdmins,np.asarray(self.backupscale)*weighting[thisOne])
          if thisData['err'] != 0:
            thisOne += 1
            self.logging.warning('error in getModisAlbedo for %s %s'%(a1Files[thisOne],a2Files[thisOne]),extra=self.d)
            # try another one?
            if thisOne == len(a1Files):
              self.logging.error('error in getModisAlbedo: No valid data files found')
              thisData['err'] = 1
              return False,0,0,0,0,nb, ns, nl, 0
          else:
            foundOne = True

        ns = thisData['ns']
        nl = thisData['nl']
        nb = thisData['nb']
        totalSnow = 0
        #set up arrays for sum, n and sum2
        self.logging.info('data allocation',extra=self.d)
        dontsnow = self.ip.dontsnow
        dontnosnow = self.ip.dontnosnow 
        dontwithsnow = self.ip.dontwithsnow 
 
        nsets = len(a1Files)
        if nsets == 0:
          self.logging.error('error in file specification: zero length list of files a1Files',extra=self.d)
          thisData['err'] = 1
          return False,0,0,0,0,nb, ns, nl, 0

        try:
          sumDataSnow = self.allocateData(nb,ns/self.ip.shrink,nl/self.ip.shrink,nsets,dontsnow)
          sumDataNoSnow = self.allocateData(nb,ns/self.ip.shrink,nl/self.ip.shrink,nsets,dontnosnow)
          sumDataWithSnow = self.allocateData(nb,ns/self.ip.shrink,nl/self.ip.shrink,nsets,dontwithsnow)
        except:
          self.logging.error('error in memory allocation: nb %d ns %d nl %d nsets %s'%(nb,ns/self.ip.shrink,nl/self.ip.shrink,nsets),extra=self.d)
          return False,0,0,0,0,nb, ns, nl, 0 

        land = np.zeros((ns,nl),dtype='bool') 
 
        if dontsnow == 0 or dontwithsnow == 0:
            totalSnow = thisData['nSnow']
            self.logging.info('n snow %d'%totalSnow,extra=self.d)

        for i in range(len(a1Files)):
            self.logging.info( 'file %d/%d'%(i,len(a1Files)),extra=self.d)
            self.logging.info( 'doy %s %s'%(str(doy[0]),str(a1Files[i])),extra=self.d)
            #only read if i > 1 as we have read first file above
            if (i != thisOne):
                thisData = self.getModisAlbedo(a1Files[i],a2Files[i],sdmins,\
                                     np.asarray(self.ip.backupscale)*weighting[i])
            if (thisData['err'] != 0):
                self.logging.warning( 'warning opening file: %s'%str(a1Files[i]),extra=self.d)
            else:
                # Lewis: sort the land info
                land = (land | (thisData['land'] == 1))
                
                self.logging.info( '... incrementing samples',extra=self.d)
                if (dontsnow == 0):
                    sumDataSnow = self.incrementSamples(sumDataSnow,thisData,1,i)
                if (dontnosnow == 0):
                    sumDataNoSnow = self.incrementSamples(sumDataNoSnow,thisData,0,i)
                if (dontwithsnow == 0):
                    sumDataWithSnow = self.incrementSamples(sumDataWithSnow,thisData,2,i)
                if (dontsnow == 0 or dontwithsnow == 0):
                    totalSnow += thisData['nSnow']
                #self.logging.info( 'done',extra=self.d)
        
    
        return True,totalSnow, sumDataNoSnow, sumDataSnow, sumDataWithSnow, nb, ns, nl, land


    def calculateStats(self,sumData):
        """
        Given data from sumdata dict, calculate mean and var/covar information

        Parameters
        -----------
        sumData : sumdata dict

        Returns
        -------

        n : float array (ns,nl)
            containing weight for sample
        meanData : float array (nb,ns,nl,3)
                   weighted mean 
        sdData : float array (nb,ns,nl,3)
                   weighted std dev


        See Also
        --------

        self.increment_samples()

        """
        focus = np.array(self.weighting)
        self.logging.info("sorting data arrays ...",extra=self.d)
        n = sumData['n']
        data = sumData['data']
        nSamples = sumData['nSamples']
        #self.logging.info("...done",extra=self.d)

        sumN = np.sum(nSamples,axis=0)
        ww = np.where(sumN>0)

        #if not (focus == 1).all():
        #  # weight the n terms
        #  for i in xrange(len(focus)):
        #    n[i,...] *= focus[i]

        total = np.sum(data*n,axis=0)
        ntot = np.sum(n,axis=0)
        ntot2 = np.sum(n**2,axis=0)
        meanData = np.zeros_like(total)
        meanData[ww] = total[ww]/ntot[ww]
        diff = data - meanData
        d2 = n*(diff**2)
        var = np.sum(d2,axis=0)
        #var[ww] /= ntot[ww]
        # small number correction: see ATBD
        num = (ntot**2 - ntot2)
        # set all valid to min err
        store = np.zeros_like(var)
        store[ww] = np.sqrt(self.minvar)
        store[var>0] = var[var>0]
        var = store
        # now fill in
        num = ntot**2 - ntot2
        ww = np.where(num>0)
        var[ww] = ntot[ww] * var[ww]/num[ww]
        # sqrt
        sdData = np.sqrt(var)
        ww = np.where(sumN>0)
        samp = sdData[ww]
        sqmax = np.sqrt(self.maxvar)
        sdData[ww][samp==0.] = np.sqrt(self.minvar)
        sdData[ww][samp>sqmax] = sqmax
          
        return ntot, meanData, sdData


    def rebin(self,a,shape):
        """
        python equivalent of IDL rebin, copied from
        stackoverflow.com/questions/8090229/resize-with-averaging-or-rebin-a-numpy-2d-array
        """
        sh = shape[0],a.shape[0]//shape[0],shape[1],a.shape[1]//shape[1]
        return a.reshape(sh).mean(-1).mean(1)


    def shrunk(self,fdata,ns,nl,shrink,sdata=None):
        """
        shrink but account for zeros

        if we specify sdata (sd) we use this in the scaling
        
        var = sdata^2
        vscale = 1./var
        fs = fdata * vscale
        
        Then shrink (mean) and normalise by vscale

        Otherwise, we normalise the mean to not count zero values in the inoput data.

        Parameters
        -----------
        fdata : float array
                data to be shrunk
        ns  : integer
               number of samples
        nl : integer
               number of lines
        shrink : integer
               shrink factor (1 for do nothing)

        sdata : float array
                sd of data


        Returns
        --------
        odata : float array
                derived from fdata but shrunk by `shrink` factor
                where shrinking produces a local mean, ignoring zero values

        Or, if ``sdata != None``, then in addition

        osdata : float array
                 Shrunk std dev array

        """
        if shrink == 1:
          if sdata != None:
            return fdata,sdata
          else:
            return fdata
        if sdata != None:
          var = sdata**2
          var[fdata==0] = 0.
          vscale = np.zeros_like(var)
          w = np.where(var>0)
          vscale[w] = 1./var[w]
          idata = fdata * vscale
          shrunkData = self.rebin(idata,(ns/shrink,nl/shrink))
          shrunkVar = self.rebin(vscale,(ns/shrink,nl/shrink))
          w = np.where(shrunkVar>0)
          odata = np.zeros_like(shrunkData)
          odata[w] = shrunkData[w]/shrunkVar[w]
          mdata = np.zeros_like(fdata)
          w1 = np.where(var>0)
          mdata[w1] = 1
          n = self.rebin(mdata,(ns/shrink,nl/shrink))
          osdata = np.zeros_like(shrunkData)
          osdata[w] = np.sqrt(n[w]/(shrunkVar[w]))
          return odata,osdata
        # else, rescale and account for zeros
        odata = self.rebin(fdata,(ns/shrink,nl/shrink))
        w = np.where(fdata == 0)
        mdata = np.ones_like(fdata)
        mdata[w] = 0
        n = self.rebin(mdata,(ns/shrink,nl/shrink))
        w2 = np.where((n > 0) & (n < 1))
        odata[w2] /= n[w2]

        return odata

    def readNetCdf(self,nb,snowType,p,doy,filename=None):
        """

        read mean and var/covar of MODIS albedo datasets to file (NetCDF format)
        filenames are of form
        OPDIR + '/' + 'Kernels.' + doy + '.' + version + '.' + tile  + '.' + Snowtype

        (or override name structure with filename option)

        Parameters
        ----------

        nb : integer
             number of bands
        snowType : string
               e.g. SnowAndNoSnow, used in filename
        p        : int -- part of image
        filename : string
               Override for defining the filename to read
        doy   : string
               dot string e.g. 001   
 
        Returns
        -------- 
 
        mean : float array
               mean array
        sd : float array
               sd  array
        n : float array
               sample weight
        l : float array
               land mask

        """
        p = '%02d'%p

        filename = filename or self.ip.opdir + '/Kernels.' + '%03d'%int(doy) + '.' + self.ip.version + '.' +\
                                 self.ip.tile + '.' + snowType +'.'+ p + '.nc'
        self.logging.info( 'reading %s'%filename,extra=self.d)
        try:
          ncfile = nc.Dataset(filename,'r')
        except:
          # maybe there is a compressed version?
          try:
            from subprocess import call
            call(['gzip','-df',filename+'.gz'])
          except:
             self.logging.info( "Failed to uncompress output file %s"%filename,extra=self.d)
        try:
          ncfile = nc.Dataset(filename,'r')
        except:
          self.logging.warning("Failed to read Netcdf file %s"%filename,extra=self.d)
          
        bNames = self.idSamples(nb)
        meanNames = np.array(bNames)[np.where(['MEAN: ' in i for i in bNames])].tolist()
        sdNames = np.array(bNames)[np.where(['SD: ' in i for i in bNames])].tolist()
        nNames = ['Weighted number of samples']
        lNames = ['land mask']
        mean = []
        for b in meanNames:
          mean.append(ncfile.variables[b][:])   
        sd = []
        for b in sdNames:
          sd.append(ncfile.variables[b][:])
        n = ncfile.variables[nNames[0][:]] 
        l = ncfile.variables[lNames[0][:]]
        return np.array(mean),np.array(sd),np.array(n),np.array(l)


    def writeNetCdf(self,ns,nl,nb,mean,sd,n,land,snowType,doy):
        """

        write mean and var/covar of MODIS albedo datasets to file (NetCDF format)
        filenames are of form
        OPDIR + '/' + 'Kernels.' + doy + '.' + version + '.' + tile  + '.' + Snowtype

        If self.ip.compression is set, an attenpt is made to compress the netCDF file.

        Parameters
        ----------

        ns : integer
             number of samples
        nl : integer
             number of lines 
        nb : integer
             number of bands
        mean : float array
               mean array
        sd : float array
               sd  array
        n : float array
               sample weight
        land : float array
               land mask
        snowType : string
               e.g. SnowAndNoSnow, used in filename
        doy  : string
             DOY string e.g. 009

        Returns
        -------

        None 

        """

        shrink = self.shrink
        doy = '%03d'%int(doy)

        filename = self.ip.opdir + '/Kernels.' + doy + '.' + self.ip.version + '.' +\
                                 self.ip.tile + '.' + snowType +'.nc'

        bNames = self.idSamples(nb)
        bNames.append('Weighted number of samples')
        bNames.append('land mask')
     
        self.logging.info( 'writing %s'%filename,extra=self.d)
        if nb == 2:
            defBands = [4,1,1]
        elif nb == 7:
            defBands = [1,14,19]
        else:
            defBands = [1,10,7]
        descrip = snowType + ' MODIS Mean/SD ' + doy + ' over the years ' + str(self.yearList)  + \
            ' version ' + self.ip.version + ' tile '+ self.ip.tile + \
            ' using input MODIS bands '
        for band in self.ip.bands:
            descrip = descrip + str(band) + ' '

        ncfile = nc.Dataset(filename,'w',format = 'NETCDF4')
        ncfile.createDimension('ns',ns)
        ncfile.createDimension('nl',nl)
        
        count = 0
        for i in range(nb):
            for j in range(3):
                data = ncfile.createVariable(bNames[count],'f4',('ns','nl'),zlib=True,least_significant_digit=10)
                data[:] = mean[j,i]
                count = count +1
                data = ncfile.createVariable(bNames[count],'f4',('ns','nl'),zlib=True,least_significant_digit=10)
                data[:] = sd[j,i]
                count = count + 1
      
        data = ncfile.createVariable(bNames[count],'f4',('ns','nl'),zlib=True,least_significant_digit=10)
        data[:] = weight
        count = count + 1
        
        data = ncfile.createVariable(bNames[count],'f4',('ns','nl'),zlib=True,least_significant_digit=2)
        data[:] = land

        setattr(ncfile,'description',descrip)
        setattr(ncfile,'data ignore value',-1.0)
        setattr(ncfile,'default bands',defBands)
 
        try: 
          ncfile.close()
        except:
          pass
        if(False and (self.ip.compression == 1)):
          try:
            from subprocess import call
            call(['gzip','-f',filename])
          except:
            self.logging.info( "Failed to compress output file %s"%filename,extra=self.d)
    

    def runAll(self):
        if os.path.exists(self.ip.opdir) == 0:
            os.makedirs(self.ip.opdir)

        self.years = self.ip.years 
        if len(self.years) == 0:
          self.years = map(int,self.yearList)

        # Lewis: try some options on data location
        self.a2list = insensitive_glob(self.ip.srcdir+'/'+'*'+self.ip.product+'2/*/%s/*/*hdf'%self.ip.tile)
        self.a1list = insensitive_glob(self.ip.srcdir+'/'+'*'+self.ip.product+'1/*/%s/*/*hdf'%self.ip.tile)

        if len(self.a2list) == 0 or len(self.a1list) == 0:
          self.a2list = insensitive_glob(self.ip.srcdir+'/'+'*'+self.ip.product+'2*hdf')
          self.a1list = insensitive_glob(self.ip.srcdir+'/'+'*'+self.ip.product+'1*hdf')
        if len(self.a2list) == 0 or len(self.a1list) == 0:
          for year in self.years:
            self.a2list.extend(insensitive_glob(self.ip.srcdir+'/'+'*'+self.ip.product+'2/%s/*.%s.*hdf'%(year,self.ip.tile)))
            self.a1list.extend(insensitive_glob(self.ip.srcdir+'/'+'*'+self.ip.product+'1/%s/*.%s.*hdf'%(year,self.ip.tile)))

        self.doyList, self.yearList = self.getDates(self.a1list)

        self.nDays = len(self.doyList)
        self.nYears = len(self.years)
         
        self.logging.info( 'N Years = %d; N Days = %d'%(self.nYears,self.nDays),extra=self.d)

        self.logging.info(str(self.doyList),extra=self.d)
        self.logging.info('file compression: %s'%str(self.ip.compression),extra=self.d)

        isRST = False
        if self.ip.rstdir != 'None':
          try:
            import pylab as plt
            isRST = True
            # try to make directory
            if os.path.exists(self.ip.rstdir) == 0:
              os.makedirs(self.ip.rstdir)
          except:
            self.logging.info("failed to load pylab or create required directory: required for rst output",extra=self.d)
            self.ip.rstdir = 'None'
            isRST = False

        #loop over each day of interest
        if (self.ip.doyList == [None]).all():
          doyList = self.doyList
        else:
          doyList = self.ip.doyList

        ok = True
        for doy in doyList:
          self.logging.info("DOY %s"%str(doy),extra=self.d)
          dimy=2400
          biny=dimy/self.ip.part

          # try reading the data from CDF files
          if self.ip.clean == False:
            for p in range(self.ip.part):
              nb = len(self.ip.bands)
              # try reading the data unless --clean
              try:
                if (self.ip.dontwithsnow == False):
                  mean, sdData, n, land = self.readNetCdf(nb,'SnowAndNoSnow',p,doy)
                  (nb,ns,nl) = mean.shape;nb/=3
                  # write RST file, but dont shrink the data again
                  if isRST:
                    self.writeRST(ns,nl,nb,mean,sdData,n,land,'SnowAndNoSnow',p,doy,shrink=1,order=1)
              except:
                ok = False
              # try reading the data unless --clean
              try:
                if (self.ip.dontsnow == False):
                  mean, sdData, n, land = self.readNetCdf(nb,'Snow',p,doy)
                  (nb,ns,nl) = mean.shape;nb/=3
                  if isRST:
                    self.writeRST(ns,nl,nb,mean,sdData,n,land,'Snow',p,doy,shrink=1,order=1)
              except:
                ok = False
              # try reading the data unless --clean
              try:
                if (self.ip.dontnosnow == False):
                  mean, sdData, n, land = self.readNetCdf(nb,'NoSnow',p,doy)
                  (nb,ns,nl) = mean.shape;nb/=3
                  if isRST:
                    self.writeRST(ns,nl,nb,mean,sdData,n,land,'NoSnow',p,doy,shrink=1,order=1)
              except:
                ok = False

          # don't do multiple parts as yet .. use --sdmims instead
          if not ok: 
           for p in range(self.ip.part):
            self.logging.info( "\n\n-------- partition %d/%d --------"%(p,self.ip.part),extra=self.d)
            y0=p*biny
            #sdimsL=[-1, -1, -1, -1]
            sdimsL = self.sdims
            if self.ip.part>1:
              sdimsL[2]=y0
              sdimsL[3]=biny
            self.logging.info("sub region: %s"%str(sdimsL),extra=self.d)

            processed,totalSnow,sumDataNoSnow,sumDataSnow,sumDataWithSnow,nb,ns,nl,land = \
                                self.processAlbedo(self.years,[doy],sdimsL)
          
            if processed: 
              land = self.shrunk(land,ns,nl,self.ip.shrink)
              ns /= self.ip.shrink
              nl /= self.ip.shrink

              self.logging.info('... calculating stats',extra=self.d)
              n = np.zeros((nb,ns,nl,3),dtype=np.float32)
              mean = np.zeros((nb,ns,nl,3),dtype=np.float32)
              sdData = np.zeros((nb,ns,nl,3),dtype=np.float32)

              if (self.ip.dontwithsnow == False):
                n, mean, sdData = self.calculateStats(sumDataWithSnow)
                self.writeNetCdf(ns,nl,nb,mean,sdData,n,land,'SnowAndNoSnow',p,doy)
                if isRST:
                  self.writeRST(ns,nl,nb,mean,sdData,n,land,'SnowAndNoSnow',p,doy)
 
              if self.ip.dontnosnow == False:
                n, mean, sdData = self.calculateStats(sumDataNoSnow)  
                self.writeNetCdf(ns,nl,nb,mean,sdData,n,land,'NoSnow',p,doy)
                if isRST:       
                  self.writeRST(ns,nl,nb,mean,sdData,n,land,'NoSnow',p,doy)
 
              if self.ip.dontsnow == False:
                n, mean, sdData = self.calculateStats(sumDataSnow)
                self.writeNetCdf(ns,nl,nb,mean,sdData,n,land,'Snow', p,doy)
                if isRST:       
                  self.writeRST(ns,nl,nb,mean,sdData,n,land,'Snow',p,doy)


def processArgs(args=None,parser=None):

    usage = "usage: %prog [options]"

    #process input arguements
    parser = parser or OptionParser(usage=usage)
    prog = 'logger'

    parser.add_option('--logfile',dest='logfile',type='string',default=None,\
                      help="set log file name") 
    parser.add_option('--logdir',dest='logdir',type='string',default='logs',\
                      help="set log directory name")
    parser.add_option('--srcdir',dest='srcdir',type='string',default='files',\
                      help="Source (MODIS MCD43) data directory")
    parser.add_option('--tile',dest='tile',type='string',default='h18v03',\
                      help="MODIS tile ID")
    parser.add_option('--backupscale',dest='backupscale',type='float',default=0.61803398875,\
                      help="Array defining the scale to map MODIS QA flags to, e.g. 0.61803398875")
    parser.add_option('--opdir',dest='opdir',type='string',default='results',\
                      help="Output directory")
    parser.add_option('--compression',dest='compression',action='store_true',default=True,\
                      help='Compress output file')
    parser.add_option('--snow',dest='snow',action='store_true',default=True,\
                      help="op snow data")
    parser.add_option('--no_snow',dest='no_snow',action='store_true',default=True,\
                      help="op no_snow data") 
    parser.add_option('--nocompression',dest='compression',action='store_false',\
                      help="Don't compress output file")
    parser.add_option('--shrink',dest='shrink',type='int',default=1,\
                      help="Spatial shrink factor (integer: default 1)")
    parser.add_option('--sdim',dest='sdim',type='string',default='[-1,-1,-1,-1]',\
                      help='image subsection: default [-1,-1,-1,-1]i or [l0,nl,s0,ns]')
    parser.add_option('--bands',dest='bands',type='string',default='[0,1,2,3,4,5,6]',help='list of bands to process. Default [0,1,2,3,4,5,6]')
    parser.add_option('--years',dest='years',type='string',default=\
                              '[2000,2001,2002,2003,2004,2005,2006,2007,2008,2009,2010,2011,2012,2013,2014,2015,2016]',help='list of years to process')
    parser.add_option('--version',dest='version',type='string',default='005',help='MODIS collection number (as string). Default 005')
    parser.add_option('--product',dest='product',type='string',default='MCD43A',help='product name (default MCD43A)')
    parser.add_option('--doy',dest='doyList',type='string',default='None',help='list of doys to process e.g. "[1,9]". N.B. do not put leading zeros on the dates (e.g. do not use e.g. [001,009])')

    
    return parser.parse_args(args or sys.argv)

if __name__ == '__main__':
    
    opts, args = processArgs()

    # Lewis: need str at times on the eval 
    opts.sdim       = np.array(ast.literal_eval(opts.sdim))
    opts.bands       = np.array(ast.literal_eval(opts.bands))
    opts.years       = np.array(ast.literal_eval(opts.years))
    opts.doyList = np.array(ast.literal_eval(opts.doyList)) 

    #class call
    self = prep_modis(opts)
    import pdb;pdb.set_trace()

    # full unique day and year list
    doyList, yearList = self.getDates(self.a1Files)

    # loop over days
    for d in doyList:
      # open output file
      ncfile = None

      # which files for these years & this doy
      a1,a2 = self.getValidFiles(yearList,d)
      dummy = False
      mask = np.zeros(len(a1)).astype(object);mask[:] = None
      for i,b in enumerate(self.bands):
        for j,(A1,A2) in enumerate(zip(a1,a2)):
          # read some data
          mask[j] = self.translate(A1,A2,bands=[b],data=mask[j])
          s0,ns,l0,nl = mask[j]['limits']

          if ncfile == None:
            snowType = (self.snow and self.no_snow and 'SnowAndNoSnow') or\
                                      (self.snow and 'Snow') or\
                                      (self.no_snow and 'NoSnow')

            filename = self.opdir + '/Kernels.' + '%03d'%int(d) + '.' + self.version + '.' +\
                                 self.tile + '.' + snowType +'.nc'

            ncfile = nc.Dataset(filename,'w',format = 'NETCDF3_CLASSIC')

            ncfile.createDimension('ns',ns)
            ncfile.createDimension('nl',nl)

            descrip = snowType + ' MODIS Mean/SD ' + d + ' over the years ' + str(yearList)  + \
                ' version ' + self.version + ' tile '+ self.tile + \
                ' using input MODIS bands '
            for band in self.bands:
              descrip = descrip + str(band) + ' '

            nb = len(self.bands)
            if nb == 2:
              defBands = [4,1,1]
            elif nb == 7:
              defBands = [1,14,19]
            else:
              defBands = [1,10,7]

            setattr(ncfile,'description',descrip)
            setattr(ncfile,'data ignore value',-1.0)
            setattr(ncfile,'default bands',defBands)

          # allocate storage
          if 'f0' not in locals():
            f0 = np.zeros((len(a1),ns,nl)).astype(np.uint16)
            f1 = f0.copy()
            f2 = f0.copy()
            snow = f0.copy().astype(bool)
            no_snow = f0.copy().astype(bool)
            land = f0.copy().astype(np.uint8)
            weight = f0.copy()

          if mask[j] == None:
            goodData = False
            if dummy == False:
              dummy = np.zeros((ns,nl)).astype(np.uint16);
            f0[j] = f1[j] = f2[j] = dummy
          else:
            goodData = True
       
            # store the reflectance data for band i
            # and year implied by A1,A2
            f0[j] = mask[j]['data'][0,0]
            f1[j] = mask[j]['data'][1,0]
            f2[j] = mask[j]['data'][2,0]
          # delete
          del mask[j]['data']
          if i == 0:
            if not goodData:
              if dummy == False:
                s0,ns,l0,nl = mask[j]['limits']
                dummy = np.zeros((ns,nl)).astype(np.uint16);
              weight[j] = dummy
              land[j] = dummy.astype(np.uint8)
              snow[j] = no_snow[j] = dummy.astype(bool)
            # store in list
            weight[j] = mask[j]['weight']
            land[j] = mask[j]['land']
            snow[j] = mask[j]['snow_mask']
            no_snow[j] = mask[j]['no_snow_mask']

        import pdb;pdb.set_trace()

        if i == 0:
          # now we have read all of the data for one band for all years
          # for the given day

          # sort the land mask (categories 1,2,3)
          landed = ma.array(land,mask=(land==0))
          tt = ma.array(np.zeros((3,ns,nl)))
          for k,t in enumerate([1,2,3]):
            tt[k] = np.sum((landed == t)*weight,axis=0)
          ttmask = tt.sum(axis=0) == 0
          # this is the summary land mask
          landed = ma.array(np.argmax(tt,axis=0)+1,mask=ttmask)
          
          ds = ncfile.createVariable('land mask','i1',('ns','nl'),zlib=True)
          ds[:] = landed

          land_mask = np.array([ttmask.mask]*weight.shape[0]).astype(bool)

          # generate 3 types of weight:
          # rescale
          weight = weight.astype(float) * 0.001  
          if self.snow and self.no_snow:
            weight = ma.array(weight,mask=~((snow|no_snow) & ~land_mask))
            sum_weight = weight.sum(axis=0)
          elif self.snow:
            weight = ma.array(weight,mask=~((snow) & ~land_mask))
            sum_weight = weight.sum(axis=0)
          elif self.no_snow:
            weight = ma.array(weight,mask=~((no_snow) & ~land_mask))
            sum_weight = weight.sum(axis=0)
       
        params   = [None]*3
        params_sd = [None]*3

        for k,f in enumerate([f0,f1,f2]):
          f = f*0.001
          f_mean = (f * weight).sum(axis=0) / sum_weight
          diff = f - f_mean
          f_var = np.sum(weight * diff * diff ,axis=0) / ( sum_weight - 1.)
          f_var[f_var<0] = np.sqrt(np.max([1.0,f_var.max()]))
          params[k], params_sd[k] = self.shrunk(f_mean,ns,nl,self.shrink,sdata=np.sqrt(f_var))

      # reset the arrays
      f0[:] = f1[:] = f2[:] = weight[:] = 0
      snow[:] = no_snow[:] = False
      land = np.array(land);land[:] = 0
      ncfile.close()


