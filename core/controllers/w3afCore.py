'''
w3afCore.py

Copyright 2006 Andres Riancho

This file is part of w3af, w3af.sourceforge.net .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

'''

import copy
import datetime
import os
import Queue
import sys
import time
import traceback

from core.controllers.coreHelpers.export import export
from core.controllers.coreHelpers.fingerprint_404 import \
    fingerprint_404_singleton
from core.controllers.coreHelpers.progress import progress
from core.controllers.misc.factory import factory
from core.controllers.misc.get_local_ip import get_local_ip
from core.controllers.misc.homeDir import (create_home_dir,
    verify_dir_has_perm, HOME_DIR)
from core.controllers.misc.number_generator import consecutive_number_generator
from core.controllers.misc.temp_dir import (create_temp_dir, remove_temp_dir,
    TEMP_DIR)
from core.controllers.targetSettings import targetSettings as targetSettings
from core.controllers.threads.threadManager import threadManagerObj as tm
from core.controllers.w3afException import (w3afException, w3afRunOnce,
    w3afFileException, w3afMustStopException, w3afMustStopByUnknownReasonExc,
    w3afMustStopOnUrlError)
from core.data.profile.profile import profile as profile
from core.data.request.frFactory import createFuzzableRequests
from core.data.url.xUrllib import xUrllib
import core.controllers.miscSettings as miscSettings
import core.controllers.outputManager as om
import core.data.kb.config as cf
import core.data.kb.knowledgeBase as kb


class w3afCore(object):
    '''
    This is the core of the framework, it calls all plugins, handles exceptions,
    coordinates all the work, creates threads, etc.
     
    @author: Andres Riancho ( andres.riancho@gmail.com )
    '''

    def __init__(self ):
        '''
        Init some variables and files.
        Create the URI opener.
        '''
        # Create some directories
        self._home_directory()
        self._tmp_directory()
        
        # Init some internal variables
        self._initializeInternalVariables()
        self._zeroSelectedPlugins()
        
        self.uriOpener = xUrllib()
        self.progress = progress()
        self.export = export()
        
        # I init the 404 detection for the whole framework
        fp_404_db = fingerprint_404_singleton()
        fp_404_db.set_urlopener( self.uriOpener )

    def _home_directory(self):
        '''
        Handle all the work related to creating/managing the home directory.
        @return: None
        '''
        # Start by trying to create the home directory (linux: /home/user/.w3af/)
        create_home_dir()

        # If this fails, maybe it is because the home directory doesn't exist
        # or simply because it ain't writable|readable by this user
        if not verify_dir_has_perm(HOME_DIR, perm=os.W_OK|os.R_OK, levels=1):
            print('Either the w3af home directory "%s" or its contents are not'
                  ' writable or readable. Please set the correct permissions '
                  'and ownership.' % HOME_DIR)
            sys.exit(-3)
            
    def _tmp_directory(self):
        '''
        Handle the creation of the tmp directory, where a lot of stuff is stored.
        Usually it's something like /tmp/w3af/<pid>/
        '''
        try:
            create_temp_dir()
        except:
            msg = ('The w3af tmp directory "%s" is not writable. Please set '
            'the correct permissions and ownership.' % TEMP_DIR)
            print msg
            sys.exit(-3)            

    def _zeroSelectedPlugins(self):
        '''
        Init some internal variables; this method is called when the whole process starts, and when the user
        loads a new profile.
        '''
        # A dict with plugin types as keys and a list of plugin names as values
        self._strPlugins = {'audit': [], 'grep': [],
                            'bruteforce': [], 'discovery': [],
                            'evasion': [], 'mangle': [], 'output': [], 'auth': []}

        self._pluginsOptions = {'audit': {}, 'grep': {}, 'bruteforce': {},
                                'discovery': {}, 'evasion': {}, 'mangle': {},
                                'output': {}, 'attack': {}, 'auth': {}}
    
    def getHomePath( self ):
        '''
        @return: The location of the w3af directory inside the home directory of the current user.
        '''
        return HOME_DIR
        
    def _initializeInternalVariables(self):
        '''
        Init some internal variables; this method is called when the whole process starts, and when the user
        performs a clear() in the gtk user interface.
        '''
        # A dict with plugin types as keys and a list of plugin instances as values
        self._plugins = {'audit':[], 'grep':[], 'bruteforce':[], 'discovery':[], \
        'evasion':[], 'mangle':[], 'output':[], 'auth': []}
        
        self._fuzzableRequestList  = []
        
        self._initialized = False
        self.target = targetSettings()
        
        # Init some values
        #
        # TODO: Can we remove this? Note that this does NOT necessary consume
        # lots of memory since items are get() by the GTK ui. The issue comes
        # when we're in the console and nobody consumes the values in the queue. 
        kb.kb.save( 'urls', 'urlQueue' ,  Queue.Queue() )
        
        self._is_running = False
        self._paused = False
        self._stopped = True
        
        # Reset global sequence number generator
        consecutive_number_generator.reset()
        
        # This indicates if we are doing discovery/audit/exploit/etc...
        self._currentPhase = ''
        # This indicates the plugin that is running right now
        self._runningPlugin = ''
        # The current fuzzable request that the core is analyzing
        self._currentFuzzableRequest = ''
        
    def _rPlugFactory( self, strReqPlugins, pluginType ):
        '''
        This method creates the requested modules list.
        
        @parameter strReqPlugins: A string list with the requested plugins to be executed.
        @parameter pluginType: [audit|discovery|grep]
        @return: A list with plugins to be executed, this list is ordered using the exec priority.
        '''     
        requestedPluginsList = []
        
        if 'all' in strReqPlugins:
            fileList = [ f for f in os.listdir('plugins' + os.path.sep+ pluginType + os.path.sep ) ]    
            allPlugins = [ os.path.splitext(f)[0] for f in fileList if os.path.splitext(f)[1] == '.py' ]
            allPlugins.remove ( '__init__' )
            
            if len ( strReqPlugins ) != 1:
                # [ 'all', '!sqli' ]
                # I want to run all plugins except sqli
                unwantedPlugins = [ x[1:] for x in strReqPlugins if x[0] =='!' ]
                strReqPlugins = list( set(allPlugins) - set(unwantedPlugins) ) #bleh! v2
            else:
                strReqPlugins = allPlugins
            
            # Update the plugin list
            # This update is usefull for cases where the user selected "all" plugins,
            # the self._strPlugins[pluginType] is useless if it says 'all'.
            self._strPlugins[pluginType] = strReqPlugins
                
        for pluginName in strReqPlugins:
            plugin = factory( 'plugins.' + pluginType + '.' + pluginName )

            # Now we are going to check if the plugin dependencies are met
            for dep in plugin.getPluginDeps():
                try:
                    depType, depPlugin = dep.split('.')
                except:
                    msg = ('Plugin dependencies must be indicated using '
                    'pluginType.pluginName notation. This is an error in '
                    '%s.getPluginDeps().' % pluginName)
                    raise w3afException(msg)
                if depType == pluginType:
                    if depPlugin not in strReqPlugins:
                        if cf.cf.getData('autoDependencies'):
                            strReqPlugins.append( depPlugin )
                            om.out.information('Auto-enabling plugin: ' + pluginType + '.' + depPlugin)
                            # nice recursive call, this solves the "dependency of dependency" problem =)
                            return self._rPlugFactory( strReqPlugins, depType )
                        else:
                            msg = ('Plugin "%s" depends on plugin "%s" and '
                            '"%s" is not enabled.' % (pluginName, dep, dep))
                            raise w3afException(msg)
                else:
                    if depPlugin not in self._strPlugins[depType]:
                        if cf.cf.getData('autoDependencies'):
                            dependObj = factory( 'plugins.' + depType + '.' + depPlugin )
                            dependObj.setUrlOpener( self.uriOpener )
                            if dependObj not in self._plugins[depType]:
                                self._plugins[depType].insert( 0, dependObj )
                                self._strPlugins[depType].append( depPlugin )
                            om.out.information('Auto-enabling plugin: ' + depType + '.' + depPlugin)
                        else:
                            msg = ('Plugin "%s" depends on plugin "%s" and '
                            '"%s" is not enabled.' % (pluginName, dep, dep))
                            raise w3afException(msg)
                    else:
                        # if someone in another planet depends on me... run first
                        self._strPlugins[depType].remove( depPlugin )
                        self._strPlugins[depType].insert( 0, depPlugin )
            
            # Now we set the plugin options
            if pluginName in self._pluginsOptions[ pluginType ]:
                pOptions = self._pluginsOptions[ pluginType ][ pluginName ]
                plugin.setOptions( pOptions )
                
            # This sets the url opener for each module that is called inside the for loop
            plugin.setUrlOpener( self.uriOpener )
            # Append the plugin to the list
            requestedPluginsList.append ( plugin )

        # The plugins are all on the requestedPluginsList, now I need to order them
        # based on the module dependencies. For example, if A depends on B , then
        # B must be run first.
        
        orderedPluginList = []
        for plugin in requestedPluginsList:
            deps = plugin.getPluginDeps()
            if len( deps ) != 0:
                # This plugin has dependencies, I should add the plugins in order
                for plugin2 in requestedPluginsList:
                    if pluginType+'.'+plugin2.getName() in deps and plugin2 not in orderedPluginList:
                        orderedPluginList.insert( 1, plugin2)

            # Check if I was added because of a dep, if I wasnt, add me.
            if plugin not in orderedPluginList:
                orderedPluginList.insert( 100, plugin )
        
        # This should never happend.
        if len(orderedPluginList) != len(requestedPluginsList):
            error_msg = ('There is an error in the way w3afCore orders '
            'plugins. The ordered plugin list length is not equal to the '
            'requested plugin list.')
            om.out.error( error_msg, newLine=False)
            
            om.out.error('The error was found sorting plugins of type: '+ pluginType +'.')
            
            error_msg = ('Please report this bug to the developers including a '
            'complete list of commands that you run to get to this error.')
            om.out.error(error_msg)

            om.out.error('Ordered plugins:')
            for plugin in orderedPluginList:
                om.out.error('- ' + plugin.getName() )

            om.out.error('\nRequested plugins:')
            for plugin in requestedPluginsList:
                om.out.error('- ' + plugin.getName() )

            sys.exit(-1)

        return orderedPluginList
    
    def initPlugins( self ):
        '''
        The user interfaces should run this method *before* calling start(). 
        If they don't do it, an exception is raised.
        '''
        self._initialized = True
        
        # This is inited before all, to have a full logging support.
        om.out.setOutputPlugins( self._strPlugins['output'] )
        
        # Create an instance of each requested plugin and add it to the plugin list
        # Plugins are added taking care of plugin dependencies and configuration

        #
        # Create the plugins that are needed during the initial discovery+bruteforce phase
        #
        self._plugins['discovery'] = self._rPlugFactory( self._strPlugins['discovery'] , 'discovery')
        self._plugins['bruteforce'] = self._rPlugFactory( self._strPlugins['bruteforce'] , 'bruteforce')        
       
        self._plugins['grep'] = self._rPlugFactory( self._strPlugins['grep'] , 'grep')
        self.uriOpener.setGrepPlugins( self._plugins['grep'] )
        
        self._plugins['mangle'] = self._rPlugFactory( self._strPlugins['mangle'] , 'mangle')
        self.uriOpener.settings.setManglePlugins( self._plugins['mangle'] )
        
        self._plugins['auth'] = self._rPlugFactory( self._strPlugins['auth'] , 'auth')

        #
        # Audit plugins are special, since they don't require to be in memory during discovery+bruteforce
        # so I'll create them here just to check that the configurations are fine and then I don't store
        # them anywhere.
        #
        self._rPlugFactory( self._strPlugins['audit'] , 'audit')


    def _update_URLs_in_KB( self, fuzzable_request_list ):
        '''
        Creates an URL list in the kb
        '''
        old_list = kb.kb.getData( 'url', 'url_objects' )
        new_list = [ fr.getURL() for fr in fuzzable_request_list if fr.getURL() not in old_list ]

        # Update the list of URLs that is used world wide
        old_list.extend( new_list )
        kb.kb.save( 'urls', 'url_objects' ,  old_list )
        
        # Update the Queue that's used in the GTK ui.
        # TODO: Can we remove this?
        urlQueue = kb.kb.getData( 'urls', 'urlQueue' )
        for u in new_list:
            urlQueue.put( u )

    def _auth_login(self):
        '''
        Make login to the web app when it is needed.
        '''
        for plugin in self._plugins['auth']:
            if not plugin.is_logged():
                plugin.login()

    def _discover_and_bruteforce( self ):
        '''
        Discovery and bruteforce phases are related, so I have joined them
        here in this method.
        '''
        go = True
        tmp_list = copy.deepcopy( self._fuzzableRequestList )
        res = []
        discovered_fr_list = []
        
        self._time_limit_reported = False
        
        while go:
            discovered_fr_list = self._discover( tmp_list )
            successfully_bruteforced = self._bruteforce( discovered_fr_list )
            if not successfully_bruteforced:
                # Haven't found new credentials
                go = False
                for fr in discovered_fr_list:
                    if fr not in res:
                        res.append( fr )
            else:
                tmp = []
                tmp.extend( discovered_fr_list )
                tmp.extend( successfully_bruteforced )
                for fr in tmp:
                    if fr not in res:
                        res.append( fr )
                
                # So in the next "while go:" loop I can do a discovery
                # using the new credentials I found
                tmp_list = successfully_bruteforced
                
                # Now I reconfigure the urllib to use the newly found credentials
                self._reconfigureUrllib()
        
        self._update_URLs_in_KB( res )
        
        return res
    
    def _reconfigureUrllib( self ):
        '''
        Configure the main urllib with the newly found credentials.
        '''
        for v in kb.kb.getData( 'basicAuthBrute' , 'auth' ):
            self.uriOpener.settings.setBasicAuth( v.getURL(), v['user'], v['pass'] )
        
        # I don't need this, the urllib2 cookie handler does this for me
        #for v in kb.kb.getData( 'formAuthBrute' , 'auth' ):
        #   self.uriOpener.settings.setHeadersList( v['additionalHeaders'] )
    
    def pause(self, pauseYesNo):
        '''
        Pauses/Un-Pauses scan.
        @parameter trueFalse: True if the UI wants to pause the scan.
        '''
        self._paused = pauseYesNo
        self._is_running = not pauseYesNo
        self._stopped = False
        self.uriOpener.pause( pauseYesNo )
        om.out.debug('The user paused/unpaused the scan.')

    def _get_time_string(self):
        '''
        @return: A string that represents in weeks/days/hours/minutes/seconds
        how much time the scan lasted.
        '''
        time_diff = time.time() - self._discovery_start_time_epoch
        time_delta = datetime.timedelta(seconds=time_diff)

        weeks, days = divmod(time_delta.days, 7)

        minutes, seconds = divmod(time_delta.seconds, 60)
        hours, minutes = divmod(minutes, 60)

        msg = ''

        if weeks == days == hours == minutes == seconds == 0:
            msg += '0 seconds.'
        else:
            if weeks:
                msg += str(weeks) + ' week%s ' % ('s' if weeks > 1 else '')
            if days:
                msg += str(days) + ' day%s ' % ('s' if days > 1 else '')
            if hours:
                msg += str(hours) + ' hour%s ' % ('s' if hours > 1 else '')
            if minutes:
                msg += str(minutes) + ' minute%s ' % ('s' if minutes > 1 else '')
            if seconds:
                msg += str(seconds) + ' second%s' % ('s' if seconds > 1 else '')
            msg += '.'
        
        return msg

        
    def start(self):
        '''
        The user interfaces call this method to start the whole scanning
        process.
        This method raises almost every possible exception, so please do your
        error handling!
        '''
        om.out.debug('Called w3afCore.start()')
        
        try:
            self._real_start()
        except MemoryError:
            msg = 'Python threw a MemoryError, this means that your'
            msg += ' OS is running very low in memory. w3af is going'
            msg += ' to stop.'
            om.out.error( msg )
            raise
        except w3afMustStopByUnknownReasonExc:
            #
            # TODO: Jan 31, 2011. Temporary workaround. Make w3af crash on
            # purpose so we can find out the *really* unknown error
            # conditions.
            #
            raise
        except w3afMustStopException, wmse:
            self._end(wmse, ignore_err=True)
            om.out.error('\n**IMPORTANT** The following error was '
             'detected by w3af and couldn\'t be resolved:\n %s\n' % wmse)
        except Exception:
            om.out.error('\nUnhandled error, traceback: %s\n' %
                         traceback.format_exc()) 
            raise
        finally:
            
            try:
                msg = 'Scan finished in %s' % self._get_time_string()
                om.out.information( msg )
            except:
                # In some cases we get here after a disk full exception
                # where the output manager can't even writea log message
                # to disk and/or the console. Seen this happen many times
                # in LiveCDs like Backtrack that don't have "real disk space"  
                pass
            
            self.progress.stop()
            
    def _real_start(self):
        '''
        Starts the work.
        User interface coders: Please remember that you have to call 
        initPlugins() method before calling start.
        
        @return: No value is returned.
        '''
        # This will help identify the total discovery time
        self._discovery_start_time_epoch = time.time()
        
        # Let the output plugins know what kind of plugins we're
        # using during the scan
        om.out.logEnabledPlugins(self._strPlugins, self._pluginsOptions)
        
        try:
            # Just in case the gtkUi / consoleUi forgot to do this...
            self.verifyEnvironment()
        except Exception, e:
            error = ('verifyEnvironment() raised an exception: "%s". This'
                ' should never happen. Are *you* user interface coder sure'
                ' that you called verifyEnvironment() *before* start() ?' % e)
            om.out.error(error)
            raise
        else:
            self._is_running = True
            self._stopped = False
            
            try:
                ###### This is the main section ######
                # Create the first fuzzableRequestList

                # We only want to scan pages that in current scope
                get_curr_scope_pages = lambda fr: \
                    fr.getURL().getDomain() == url.getDomain()

                for url in cf.cf.getData('targets'):
                    try:
                        #
                        #    GET the initial target URLs in order to save them
                        #    in a list and use them as our bootstrap URLs
                        #
                        response = self.uriOpener.GET(url, useCache=True)
                        self._fuzzableRequestList += filter(
                            get_curr_scope_pages, createFuzzableRequests(response))

                        #
                        #    NOTE: I need to perform this test here in order to avoid some weird
                        #    thread locking that happens when the webspider calls is_404, and
                        #    because I want to initialize the is_404 database in a controlled
                        #    try/except block.
                        #
                        from core.controllers.coreHelpers.fingerprint_404 import is_404
                        is_404(response)

                    except KeyboardInterrupt:
                        self._end()
                        raise
                    except (w3afMustStopOnUrlError, w3afException, w3afMustStopException), w3:
                        om.out.error('The target URL: %s is unreachable.' % url)
                        om.out.error('Error description: %s' % w3)
                    except Exception, e:
                        om.out.error('The target URL: %s is unreachable '
                                     'because of an unhandled exception.' % url)
                        om.out.error('Error description: "%s". See debug '
                                     'output for more information.' % e)
                        om.out.error('Traceback for this error: %s' % 
                                     traceback.format_exc())
                
                # Load the target URLs to the KB
                self._update_URLs_in_KB( self._fuzzableRequestList )
                
                self._fuzzableRequestList = self._discover_and_bruteforce()
                
                # Export all fuzzableRequests as CSV
                # if this option is set in the miscSettings
                if cf.cf.getData('exportFuzzableRequests') != '':
                    self.export.exportFuzzableRequestList(self._fuzzableRequestList)
                    
                if not self._fuzzableRequestList:
                    om.out.information('No URLs found during discovery phase.')
                else:
                    # Remove the discovery and bruteforce plugins from memory
                    # This is a performance enhancement.
                    self._plugins['discovery'] = []
                    self._plugins['bruteforce'] = []

                    # Sort URLs
                    tmp_url_list = kb.kb.getData( 'urls', 'url_objects')[:]
                    tmp_url_list = list(set(tmp_url_list))
                    tmp_url_list.sort()
                    
                    msg = 'Found %s URLs and %s different points of injection.' 
                    msg = msg % (len(tmp_url_list), len(self._fuzzableRequestList))
                    om.out.information( msg )
                    
                    # print the URLs
                    om.out.information('The list of URLs is:')
                    for i in tmp_url_list:
                        om.out.information( '- ' + i )

                    # Filter out the fuzzable requests that aren't important 
                    # (and will be ignored by audit plugins anyway...)
                    #
                    #   What I want to do here, is filter the repeated fuzzable requests.
                    #   For example, if the spidering process found:
                    #       - http://host.tld/?id=3739286
                    #       - http://host.tld/?id=3739285
                    #       - http://host.tld/?id=3739282
                    #       - http://host.tld/?id=3739212
                    #
                    #   I don't want to have all these different fuzzable requests. The reason is that
                    #   audit plugins will try to send the payload to each parameter, thus generating
                    #   the following requests:
                    #       - http://host.tld/?id=payload1
                    #       - http://host.tld/?id=payload1
                    #       - http://host.tld/?id=payload1
                    #       - http://host.tld/?id=payload1
                    #
                    #   w3af has a cache, but its still a waste of time to send those requests.
                    #
                    #   Now lets analyze this with more than one parameter. Spidered URIs:
                    #       - http://host.tld/?id=3739286&action=create
                    #       - http://host.tld/?id=3739285&action=create
                    #       - http://host.tld/?id=3739282&action=remove
                    #       - http://host.tld/?id=3739212&action=remove
                    #
                    #   Generated requests:
                    #       - http://host.tld/?id=payload1&action=create
                    #       - http://host.tld/?id=3739286&action=payload1
                    #       - http://host.tld/?id=payload1&action=create
                    #       - http://host.tld/?id=3739285&action=payload1
                    #       - http://host.tld/?id=payload1&action=remove
                    #       - http://host.tld/?id=3739282&action=payload1
                    #       - http://host.tld/?id=payload1&action=remove
                    #       - http://host.tld/?id=3739212&action=payload1
                    #
                    #   In cases like this one, I'm sending these repeated requests:
                    #       - http://host.tld/?id=payload1&action=create
                    #       - http://host.tld/?id=payload1&action=create
                    #       - http://host.tld/?id=payload1&action=remove
                    #       - http://host.tld/?id=payload1&action=remove
                    #   But there is not much I can do about it... (except from having a nice cache)
                    #
                    #   TODO: Is the previous statement completely true?
                    #
                    '''filtered_fuzzable_requests = []
                    for fr_original in self._fuzzableRequestList:
                        
                        different_from_all = True
                        
                        for fr_filtered in filtered_fuzzable_requests:
                            if fr_filtered.is_variant_of( fr_original ):
                                different_from_all = False
                                break
                        
                        if different_from_all:
                            filtered_fuzzable_requests.append( fr_original )
                    
                    self._fuzzableRequestList = filtered_fuzzable_requests
                    '''
                    
                    # Now I simply print the list that I have after the filter.
                    tmp_fr = [ '- ' + str(fr) for fr in self._fuzzableRequestList]
                    tmp_fr.sort()

                    om.out.information('The list of fuzzable requests is:')
                    map(om.out.information, tmp_fr)
                    
                    self._audit()
                    
                self._end()
                ###########################
            
            except w3afFileException, e:
                self._end(e)
                om.out.setOutputPlugins( ['console'] )
            except w3afException, e:
                self._end(e)
                raise
            except KeyboardInterrupt, e:
                self._end()
                # I wont handle this. 
                # The user interface must know what to do with it
                raise
    
    def cleanup( self ):
        '''
        The GTK user interface calls this when a scan has been stopped 
        (or ended successfully) and the user wants to start a new scan.
        All data from the kb is deleted.
        
        @return: None
        '''
        # Clean all data that is stored in the kb
        kb.kb.cleanup()

        # Zero internal variables from the core
        self._initializeInternalVariables()
        
        # Not cleaning the config is a FEATURE, because the user is most likely going to start a new
        # scan to the same target, and he wants the proxy, timeout and other configs to remain configured
        # as he did it the first time.
        # reload(cf)
        
        # It is also a feature to keep the mist settings from the last run.
        # Set some defaults for the core
        #import core.controllers.miscSettings as miscSettings
        #miscSettings.miscSettings()
        
        # Not calling:
        # self._zeroSelectedPlugins()
        # because I wan't to keep the selected plugins and configurations
        
    def stop( self ):
        '''
        This method is called by the user interface layer, when the user "clicks" on the stop button.
        @return: None. The stop method can take some seconds to return.
        '''
        om.out.debug('The user stopped the core.')
        # Stop sending HTTP requests
        self.uriOpener.stop()
        
        # End the grep plugins
        self._end(ignore_err=True)
    
    def quit( self ):
        '''
        The user is in a hurry, he wants to exit w3af ASAP.
        '''
        # Stop sending HTTP requests
        self.uriOpener.stop()
        
        # End the grep plugins
        #self._end()
        
        # Now it's safe to remove the temp_dir
        remove_temp_dir()
        
    def _end(self, exc_inst=None, ignore_err=False):
        '''
        This method is called when the process ends normally or by an error.
        '''
        try:
            # End the xUrllib (clear the cache)
            self.uriOpener.end()
            # Create a new one, so it can be used by exploit plugins.
            self.uriOpener = xUrllib()
            
            # Silently ignore. w3af is stopped
            try:
                # Let the progress module know our status.
                self.progress.stop()
            except:
                pass
            
            if exc_inst:
                om.out.debug(str(exc_inst))
            
            tm.join(joinAll=True)
            tm.stopAllDaemons()
            
            for plugin in self._plugins['grep']:
                plugin.end()
            
            # Also, close the output manager.
            om.out.endOutputPlugins()
        except Exception, ex:
            if not ignore_err:
                raise
        finally:
            # Now I'm definitly not running:
            self._is_running = False
            self._stopped = True
            
            # No targets to be scanned.
            cf.cf.save('targets', [])
        
    def isRunning( self ):
        '''
        @return: If the user has called start, and then wants to know if the
        core is still working, it should call isRunning to know that.
        '''
        return self._is_running
    
    def _discover(self, toWalk):
        # Init some internal variables
        self._alreadyWalked = toWalk
        self._set_phase('discovery')
        
        result = []
        try:
            result = self._discoverWorker( toWalk )
        except KeyboardInterrupt:
            om.out.information('The user interrupted the discovery phase, '
                               'continuing with audit.')
            result = self._alreadyWalked
        
        # Let the plugins know that they won't be used anymore
        self._end_discovery()
        
        return result
    
    def _end_discovery( self ):
        '''
        Let the discovery plugins know that they won't be used anymore.
        '''
        for p in self._plugins['discovery']:
            try:
                p.end()
            except Exception, e:
                om.out.error('The plugin "%s" raised an exception in the '
                             'end() method: %s' % (p.getName(), e))
    
    def get_discovery_time(self):
        '''
        @return: The time between now and the start of the discovery phase in
            minutes.
        '''
        now = time.time()
        diff = now - self._discovery_start_time_epoch
        return diff / 60
    
    def _discoverWorker(self, toWalk):
        om.out.debug('Called _discoverWorker()' )
        
        while toWalk:
            
            # Progress stuff, do this inside the while loop, because the toWalk variable changes
            # in each loop
            amount_of_tests = len(self._plugins['discovery']) * len(toWalk)
            self.progress.set_total_amount(amount_of_tests)
            
            plugins_to_remove_list = []
            fuzz_reqs = {}
            
            for plugin in self._plugins['discovery']:
                
                # Login is needed,
                self._auth_login()
                
                # Using the self._time_limit_reported variable to break
                # out of two loops
                if self._time_limit_reported:
                    break
                    
                for fr in toWalk:
                    # Time exceeded?
                    if self.get_discovery_time() > cf.cf.getData('maxDiscoveryTime'):
                        if not self._time_limit_reported:
                            # Using self._time_limit_reported variable to
                            # break out of two loops
                            self._time_limit_reported = True
                            om.out.information('Maximum discovery time limit hit.')
                        
                        # Replaced the return [] with this break to avoid
                        # losing all the gathered knowledge.
                        break

                    self._setRunningPlugin(plugin.getName())
                    self._setCurrentFuzzableRequest(fr)
                    try:
                        try:
                            # Perform the actual work
                            pluginResult = plugin.discover_wrapper(fr)
                        finally:
                            tm.join(plugin)
                    except w3afException,e:
                        om.out.error(str(e))
                    except w3afRunOnce:
                        # Some plugins are ment to be run only once
                        # that is implemented by raising a w3afRunOnce
                        # exception
                        plugins_to_remove_list.append(plugin)
                    else:
                        # We don't trust plugins, i'll only work if this
                        # is a list or something else that is iterable
                        lst = fuzz_reqs.setdefault(plugin.getName(), [])
                        if hasattr(pluginResult, '__iter__'):
                            lst.extend(r for r in pluginResult)
                                
                    om.out.debug('Ending plugin: ' + plugin.getName())
                    
                    # Finished one loop, inc!
                    self.progress.inc()
                    
                    # If the user wants to stop, I have to stop!
                    if self._stopped:
                        return []
            
            # The search has finished - now performing some mangling
            # with the requests
            new_fuzz_reqs = []
            for pname, fuzzables in fuzz_reqs.items():
                
                for fuzz in fuzzables:
                    # No need to care about fragments
                    # (http://a.com/foo.php#frag). Remove them
                    fuzz.setURI(fuzz.getURI().removeFragment())
                    
                    if fuzz not in self._alreadyWalked and \
                        fuzz.getURL().baseUrl() in cf.cf.getData('baseURLs'):
                        # Found a new fuzzable request
                        new_fuzz_reqs.append(fuzz)
                        self._alreadyWalked.append(fuzz)
            
                # Print the new URLs in a sorted manner.
                for url in sorted(
                    set(fr.getURL().url_string for fr in new_fuzz_reqs)
                ):
                    om.out.information(
                        'New URL found by %s plugin: %s' % (pname, url))

            # Update the list / queue that lives in the KB
            self._update_URLs_in_KB(new_fuzz_reqs)

            # Cleanup stuff
            del fuzz_reqs
            try:
                del fuzz
            except:
                pass
            
            # Get ready for next while loop
            toWalk = new_fuzz_reqs
            
            # Remove plugins that don't want to be run anymore
            for plugin_to_remove in plugins_to_remove_list:
                if plugin_to_remove in self._plugins['discovery']:
                    
                    # Remove it from the plugin list, and run the end() method
                    self._plugins['discovery'].remove( plugin_to_remove )
                    om.out.debug('The discovery plugin: ' + plugin_to_remove.getName() + ' wont be run anymore.')      
                    try:
                        plugin_to_remove.end()
                    except Exception, e:
                        msg = 'The plugin "'+ plugin_to_remove.getName() + '" raised an exception'
                        msg += ' in the end() method: ' + str(e)
                        om.out.error( msg )
                    
        return self._alreadyWalked
    
    ######## These methods are here to show a detailed information of what the core is doing ############
    
    def getCoreStatus( self ):
        if self._paused:
            return 'Paused.'
        elif self._stopped:
            return 'Not running.'
        else:
            if self.getPhase() != '' and self.getRunningPlugin() != '':
                running = 'Running ' + self.getPhase() + '.' + self.getRunningPlugin()
                running += ' on ' + str(self.getCurrentFuzzableRequest()).replace('\x00', '') + '.'
                return running
            else:
                return 'Starting scan.'
    
    def getPhase( self ):
        '''
        @return: The phase which the core is running.
        '''
        return self._currentPhase
        
    def _set_phase( self, phase ):
        '''
        This method saves the phase (discovery/audit/exploit), so in the future the UI can use the getPhase() method to show it.
        
        @parameter phase: The phase which the w3afCore is running in a given moment
        '''
        self._currentPhase = phase
    
    def _setRunningPlugin( self, pluginName ):
        '''
        This method saves the phase, so in the future the UI can use the getPhase() method to show it.
        
        @parameter pluginName: The pluginName which the w3afCore is running in a given moment
        '''
        om.out.debug('Starting plugin: ' + pluginName )
        self._runningPlugin = pluginName
        
    def getRunningPlugin( self ):
        '''
        @return: The plugin that the core is running when the method is called.
        '''
        return self._runningPlugin
        
    def getCurrentFuzzableRequest( self ):
        '''
        @return: The current fuzzable request that the w3afCore is working on.
        '''
        return self._currentFuzzableRequest
        
    def _setCurrentFuzzableRequest( self, fuzzableRequest ):
        '''
        @parameter fuzzableRequest: The fuzzableRequest that the w3afCore is working on right now.
        '''
        self._currentFuzzableRequest = fuzzableRequest
    ######## end of: methods that are here to show a detailed information of what the core is doing ############
    
    def _audit(self):
        om.out.debug('Called _audit()' )

        audit_plugins = self._rPlugFactory( self._strPlugins['audit'] , 'audit')

        # For progress reporting
        self._set_phase('audit')
        amount_of_tests = len(audit_plugins) * len(self._fuzzableRequestList)
        self.progress.set_total_amount( amount_of_tests )

        # Put everything in a queue and remove the audit_plugins list
        audit_queue = Queue.Queue()
        for audit_plugin in audit_plugins:
            audit_queue.put( audit_plugin )
        del(audit_plugins)

        
        # This two loops do all the audit magic [KISS]
        while not audit_queue.empty():

            # Get the next plugin from the queue
            plugin = audit_queue.get()
            
            # For status
            self._setRunningPlugin( plugin.getName() )

            # Before running each plugin let's make sure we're logged in
            self._auth_login()

            for fr in self._fuzzableRequestList:
                # Sends each fuzzable request to the plugin
                try:
                    self._setCurrentFuzzableRequest( fr )
                    plugin.audit_wrapper( fr )
                except w3afException, e:
                    om.out.error( str(e) )
                finally:
                    tm.join( plugin )
                
                # I performed one test
                self.progress.inc()
                    
            # Let the plugin know that we are not going to use it anymore
            try:
                plugin.end()
            except w3afException, e:
                om.out.error( str(e) )
            
    def _bruteforce(self, fuzzableRequestList):
        '''
        @parameter fuzzableRequestList: A list of fr's to be analyzed by the bruteforce plugins
        @return: A list of the URL's that have been successfully bruteforced
        '''
        res = []
        
        # Progress
        om.out.debug('Called _bruteforce()' )
        self._set_phase('bruteforce')
        amount_of_tests = len(self._plugins['bruteforce']) * len(fuzzableRequestList)
        self.progress.set_total_amount( amount_of_tests )
        
        for plugin in self._plugins['bruteforce']:
            # FIXME: I should remove this information lines, they duplicate functionality with the setRunningPlugin
            om.out.information('Starting ' + plugin.getName() + ' plugin execution.')
            self._setRunningPlugin( plugin.getName() )
            for fr in fuzzableRequestList:
                
                # Sends each url to the plugin
                try:
                    self._setCurrentFuzzableRequest( fr )
                    
                    frList = plugin.bruteforce_wrapper( fr )
                    tm.join( plugin )
                except w3afException, e:
                    tm.join( plugin )
                    om.out.error( str(e) )
                    
                # I performed one test (no matter if it failed or not)
                self.progress.inc()                    
                    
                try:
                    plugin.end()
                except w3afException, e:
                    om.out.error( str(e) )
                    
                res.extend( frList )
                
        return res

    def setPluginOptions(self, pluginType, pluginName, pluginOptions):
        '''
        @parameter pluginType: The plugin type, like 'audit' or 'discovery'
        @parameter pluginName: The plugin name, like 'sqli' or 'webSpider'
        @parameter pluginOptions: An optionList object with the option objects for a plugin.
        
        @return: No value is returned.
        '''
        if pluginType.lower() == 'output':
            om.out.setPluginOptions(pluginName, pluginOptions)
            
        # The following lines make sure that the plugin will accept the options
        # that the user is setting to it.
        pI = self.getPluginInstance(pluginName, pluginType)
        try:
            pI.setOptions(pluginOptions)
        except Exception:
            raise
        else:
            # Now that we are sure that these options are valid, lets save them
            # so we can use them later!
            self._pluginsOptions[pluginType][pluginName] = pluginOptions

    def getPluginOptions(self, pluginType, pluginName):
        '''
        Get the options for a plugin.
        
        IMPORTANT NOTE: This method only returns the options for a plugin
        that was previously configured using setPluginOptions. If you wan't
        to get the default options for a plugin, get a plugin instance and
        perform a plugin.getOptions()
        
        @return: An optionList with the plugin options.
        '''
        return self._pluginsOptions.get(pluginType, {}).get(pluginName, None)
        
    def getEnabledPlugins( self, pluginType ):
        return self._strPlugins[ pluginType ]
    
    def setPlugins( self, pluginNames, pluginType ):
        '''
        This method sets the plugins that w3afCore is going to use. Before this plugin
        existed w3afCore used setDiscoveryPlugins() / setAuditPlugins() / etc , this wasnt
        really extensible and was replaced with a combination of setPlugins and getPluginTypes.
        This way the user interface isnt bound to changes in the plugin types that are added or
        removed.
        
        @parameter pluginNames: A list with the names of the Plugins that will be run.
        @parameter pluginType: The type of the plugin.
        
        @return: A list of plugins that are unknown to the framework. This is mainly used to have
        some error handling related to old profiles, that might reference deprecated plugins.
        '''
        unknown_plugins = []
        
        # Validate the input...
        pluginNames = list( set( pluginNames ) )    # bleh !
        pList = self.getPluginList(  pluginType  )
        for p in pluginNames:
            if p not in pList and p.replace('!','') not in pList and p != 'all':
                unknown_plugins.append( p )
        
        setMap = {
            'discovery': self._setDiscoveryPlugins,
            'audit': self._setAuditPlugins,
            'grep': self._setGrepPlugins,
            'evasion': self._setEvasionPlugins,
            'output': self._setOutputPlugins,
            'mangle': self._setManglePlugins,
            'bruteforce': self._setBruteforcePlugins,
            'auth': self._setAuthPlugins
            }
        
        func = setMap[pluginType]
        func(pluginNames)
        
        return unknown_plugins
    
    def reloadModifiedPlugin(self,  pluginType,  pluginName):
        '''
        When a plugin is modified using the plugin editor, all instances of it inside the core have to be "reloaded"
        so, if the plugin code was changed, the core reflects that change.
        
        @parameter pluginType: The plugin type of the modified plugin ('audit','discovery', etc)
        @parameter pluginName: The plugin name of the modified plugin ('xss', 'sqli', etc)
        '''
        try:
            aModule = sys.modules['plugins.' + pluginType + '.' + pluginName ]
        except KeyError:
            om.out.debug('Tried to reload a plugin that was never imported! ('+ pluginType +'.' + pluginName + ')')
        else:
            reload(aModule)
    
    def getPluginTypesDesc( self, pluginType ):
        '''
        @parameter pluginType: The type of plugin for which we want a description.
        @return: A description of the plugin type passed as parameter
        '''
        try:
            __import__('plugins.' + pluginType )
            aModule = sys.modules['plugins.' + pluginType ]
        except Exception, e:
            raise w3afException('Unknown plugin type: "'+ pluginType + '".')
        else:
            return aModule.getLongDescription()
        
    def getPluginTypes(self):
        '''
        @return: A list with all plugin types.
        '''
        def rem_from_list(ele, lst):
            try:
                lst.remove(ele)
            except:
                pass
        pluginTypes = [x for x in os.listdir('plugins' + os.path.sep)]
        # Now we filter to show only the directories
        pluginTypes = [d for d in pluginTypes 
                       if os.path.isdir(os.path.join('plugins', d))]
        rem_from_list('attack', pluginTypes)
        rem_from_list('tests', pluginTypes)
        rem_from_list('.svn', pluginTypes)
        return pluginTypes
    
    def _setBruteforcePlugins( self, bruteforcePlugins ):
        '''
        @parameter manglePlugins: A list with the names of output Plugins that will be run.
        @return: No value is returned.
        '''
        self._strPlugins['bruteforce'] = bruteforcePlugins
    
    def _setManglePlugins( self, manglePlugins ):
        '''
        @parameter manglePlugins: A list with the names of output Plugins that will be run.
        @return: No value is returned.
        '''
        self._strPlugins['mangle'] = manglePlugins
    
    def _setOutputPlugins( self, outputPlugins ):
        '''
        @parameter outputPlugins: A list with the names of output Plugins that will be run.
        @return: No value is returned.
        '''
        self._strPlugins['output'] = outputPlugins
        
    def _setDiscoveryPlugins( self, discoveryPlugins ):
        '''
        @parameter discoveryPlugins: A list with the names of Discovery Plugins that will be run.
        @return: No value is returned.
        '''         
        self._strPlugins['discovery'] = discoveryPlugins
    
    def _setAuditPlugins( self, auditPlugins ):
        '''
        @parameter auditPlugins: A list with the names of Audit Plugins that will be run.
        @return: No value is returned.
        '''         
        self._strPlugins['audit'] = auditPlugins
        
    def _setGrepPlugins( self, grepPlugins):
        '''
        @parameter grepPlugins: A list with the names of Grep Plugins that will be used.
        @return: No value is returned.
        '''     
        self._strPlugins['grep'] = grepPlugins
        
    def _setEvasionPlugins( self, evasionPlugins ):
        '''
        @parameter evasionPlugins: A list with the names of Evasion Plugins that will be used.
        @return: No value is returned.
        '''
        self._strPlugins['evasion'] = evasionPlugins
        self._plugins['evasion'] = self._rPlugFactory( evasionPlugins , 'evasion')
        self.uriOpener.setEvasionPlugins( self._plugins['evasion'] )
        
    def _setAuthPlugins( self, authPlugins ):
        '''
        @parameter authlugins: A list with the names of Auth Plugins that will be used.
        @return: No value is returned.
        '''
        self._strPlugins['auth'] = authPlugins

    def verifyEnvironment(self):
        '''
        Checks if all parameters where configured correctly by the above layer (w3af.py)
        '''
        # Init ALL plugins
        if not self._initialized:
            raise w3afException('You must call the initPlugins method before calling start()')
        
        try:
            assert cf.cf.getData('targets')  != [], 'No target URI configured.'
        except AssertionError, ae:
            raise w3afException( str(ae) )
            
        try:
            cry = True
            if len(self._strPlugins['audit']) == 0 and len(self._strPlugins['discovery']) == 0 \
            and len(self._strPlugins['grep']) == 0:
                cry = False
            assert cry , 'No audit, grep or discovery plugins configured to run.'
        except AssertionError, ae:
            raise w3afException( str(ae) )
    
    def getPluginList( self, pluginType ):
        '''
        @return: A string list of the names of all available plugins by type.
        '''
        strPluginList = self._getListOfFiles( 'plugins' + os.path.sep + pluginType + os.path.sep )
        return strPluginList
        
    def getProfileList( self ):
        '''
        @return: Two different lists:
        
            - One that contains the instances of the valid profiles that were loaded
            - One with the file names of the profiles that are invalid
        '''
        profile_home = os.path.join(HOME_DIR, 'profiles')
        str_profile_list = self._getListOfFiles(profile_home, extension='.pw3af')
        
        instance_list = []
        invalid_profiles = []
        
        for profile_name in str_profile_list:
            profile_filename = os.path.join(profile_home, profile_name + '.pw3af')
            try:
                profile_instance = profile( profile_filename )
            except:
                invalid_profiles.append( profile_filename )
            else:
                instance_list.append( profile_instance )
        return instance_list, invalid_profiles
        
    def _getListOfFiles( self, directory, extension='.py' ):
        '''
        @return: A string list of the names of all available plugins by type.
        '''
        strFileList = []
        
        for f in os.listdir(directory):
            fname, ext = os.path.splitext(f)
            if ext == extension and fname != '__init__':
                strFileList.append(fname)

        strFileList.sort()
        return strFileList
        
    def getPluginInstance(self, pluginName, pluginType):
        '''
        @return: An instance of a plugin.
        '''
        pluginInst = factory('plugins.' + pluginType + '.' + pluginName)
        pluginInst.setUrlOpener(self.uriOpener)
        if pluginName in self._pluginsOptions[ pluginType ].keys():
            pluginInst.setOptions(self._pluginsOptions[pluginType ][pluginName])
        
        # This will init some plugins like mangle and output
        if pluginType == 'attack' and not self._initialized:
            self.initPlugins()
        return pluginInst
    
    def saveCurrentToNewProfile( self, profile_name, profileDesc='' ):
        '''
        Saves current config to a newly created profile.
        
        @parameter profile_name: The profile to clone
        @parameter profileDesc: The description of the new profile
        
        @return: The new profile instance if the profile was successfully saved. Else, raise a w3afException.
        '''
        # Create the new profile.
        profileInstance = profile()
        profileInstance.setDesc( profileDesc )
        profileInstance.setName( profile_name )
        profileInstance.save( profile_name )
        
        # Save current to profile
        return self.saveCurrentToProfile( profile_name, profileDesc )

    def saveCurrentToProfile(self, profile_name, prof_desc='', prof_path=''):
        '''
        Save the current configuration of the core to the profile called 
        profile_name.
        
        @return: The new profile instance if the profile was successfully saved.
            otherwise raise a w3afException.
        '''
        # Open the already existing profile
        new_profile = profile(profile_name, workdir=os.path.dirname(prof_path))
        
        # Config the enabled plugins
        for pType in self.getPluginTypes():
            enabledPlugins = []
            for pName in self.getEnabledPlugins(pType):
                enabledPlugins.append( pName )
            new_profile.setEnabledPlugins(pType, enabledPlugins)
        
        # Config the profile options
        for pType in self.getPluginTypes():
            for pName in self.getEnabledPlugins(pType):
                pOptions = self.getPluginOptions(pType, pName)
                if pOptions:
                    new_profile.setPluginOptions(pType, pName, pOptions)
                
        # Config the profile targets
        targets = cf.cf.getData('targets')
        if targets:
            new_profile.setTarget(' , '.join(t.url_string for t in targets))
        
        # Config the misc and http settings
        misc_settings = miscSettings.miscSettings()
        new_profile.setMiscSettings(misc_settings.getOptions())
        new_profile.setHttpSettings(self.uriOpener.settings.getOptions())
        
        # Config the profile name and description
        new_profile.setDesc(prof_desc)
        new_profile.setName(profile_name)
        
        # Save the profile to the file
        new_profile.save(profile_name)
        
        return new_profile
        
    def removeProfile( self, profile_name ):
        '''
        @return: True if the profile was successfully removed. Else, raise a w3afException.
        '''
        profileInstance = profile( profile_name )
        profileInstance.remove()
        return True
        
    def useProfile(self, profile_name, workdir=None):
        '''
        Gets all the information from the profile, and runs it.
        Raise a w3afException if the profile to load has some type of problem.
        '''
        # Clear all enabled plugins if profile_name is None
        if profile_name is None:
            self._zeroSelectedPlugins()
            return
        
        try:            
            profileInstance = profile(profile_name, workdir) 
        except w3afException:
            # The profile doesn't exist!
            raise
        else:
            # It exists, work with it!
            for pluginType in self._plugins.keys():
                pluginNames = profileInstance.getEnabledPlugins( pluginType )
                
                # Handle errors that might have been triggered from a possibly invalid profile
                unknown_plugins = self.setPlugins( pluginNames, pluginType )
                if unknown_plugins:
                    om.out.error('The profile references the following missing plugins:')
                    for unknown_plugin_name in unknown_plugins:
                        om.out.error('- ' + unknown_plugin_name)
                    
                # Now we set the plugin options, which can also trigger errors with "outdated"
                # profiles that users could have in their ~/.w3af/ directory.
                for pluginName in profileInstance.getEnabledPlugins( pluginType ):
                    pluginOptions = profileInstance.getPluginOptions( pluginType, pluginName )
                    try:
                        # FIXME: Does this work with output plugin options?
                        # What about target, http-settings, etc?
                        self.setPluginOptions( pluginType, pluginName, pluginOptions )
                    except Exception, e:
                        # This is because of an invalid plugin, or something like that...
                        # Added as a part of the fix of bug #1937272
                        msg = ('The profile you are trying to load seems to be'
                        ' outdated, one of the enabled plugins has a bug or an'
                        ' plugin option that was valid when you created the '
                        'profile was now removed from the framework. The plugin'
                        ' that triggered this exception is "%s", and the '
                        'original exception is: "%s"' % (pluginName, e))
                        om.out.error(msg)
                    
            # Set the target settings of the profile to the core
            self.target.setOptions( profileInstance.getTarget() )
            
            # Set the misc and http settings
            #
            # IGNORE the following parameters from the profile:
            #   - miscSettings.localAddress
            #
            profile_misc_settings = profileInstance.getMiscSettings()
            if 'localAddress' in profileInstance.getMiscSettings():
                profile_misc_settings['localAddress'].setValue(get_local_ip())
            
            misc_settings = miscSettings.miscSettings()
            misc_settings.setOptions( profile_misc_settings )
            self.uriOpener.settings.setOptions( profileInstance.getHttpSettings() )
    
# Singleton
wCore = w3afCore()