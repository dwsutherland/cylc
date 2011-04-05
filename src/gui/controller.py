#!/usr/bin/env python

import gtk
import pygtk
#pygtk.require('2.0')
import pango
import os, re
import Pyro.errors
import gobject
import subprocess
import helpwindow
from stateview import updater
from combo_logviewer import combo_logviewer
from warning_dialog import warning_dialog, info_dialog
from port_scan import SuiteIdentificationError
import cylc_pyro_client
from cycle_time import is_valid
from execute import execute
from option_group import controlled_option_group
from config import config
from color_rotator import rotator
from cylc_logviewer import cylc_logviewer
from textload import textload
from cylc_xdot import xdot_widgets

class ControlApp(object):

    def __init__(self, suite, owner, host, port, suite_dir, logging_dir, imagedir, readonly=False ):
        self.readonly = readonly
        self.logdir = logging_dir
        self.suite_dir = suite_dir
        self.tfilt = ''

        self.suite = suite
        self.host = host
        self.port = port
        self.owner = owner
        self.imagedir = imagedir
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        #self.window.set_border_width( 5 )
        if self.readonly:
            self.window.set_title("cylc view <" + self.suite + "> (READONLY)" )
        else:
            self.window.set_title("gcylc <" + self.suite + ">" )
        self.window.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( "#ddd" ))
        self.window.set_size_request(800, 500)
        self.window.connect("delete_event", self.delete_event)

        self.log_colors = rotator()

        # Get list of tasks in the suite
        self.preload_task_list()

        self.create_menu()

        notebook = gtk.Notebook()
        notebook.set_tab_pos(gtk.POS_TOP)
 
        bigbox = gtk.VBox()
        bigbox.pack_start( self.menu_bar, False )
        hbox = gtk.HBox()
        hbox.pack_start( self.create_info_bar(), True )
        bigbox.pack_start( hbox, False )

        main_panes = gtk.VPaned()
        main_panes.set_position(200)
        #main_panes.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#d91212' ))
        main_panes.add1( self.ledview_widgets())
        main_panes.add2( self.treeview_widgets())

        self.graphw = xdot_widgets()
        self.graphw.widget.connect( 'clicked', self.on_url_clicked )
        self.graphw.graph_disconnect_button.connect( 'toggled', self.toggle_graph_disconnect )

        self.quitters = []
        self.connection_lost = False
        self.t = updater( self.suite, self.owner, self.host, self.port,
                self.imagedir, self.led_treeview.get_model(),
                self.ttreeview, self.task_list, self.label_mode,
                self.label_status, self.label_time, self.graphw )

        self.full_task_headings()
        #print "Starting task state info thread"
        self.t.start()

        notebook.append_page( main_panes, gtk.Label('traditional'))
        notebook.append_page(self.graphw.get(), gtk.Label('live graph'))

        bigbox.pack_start( notebook, True )
        self.window.add( bigbox )
        self.window.show_all()

    def toggle_graph_disconnect( self, w ):
        if w.get_active():
            self.t.graph_disconnect = True
            w.set_label( 'REconnect' )
        else:
            self.t.graph_disconnect = False
            w.set_label( 'DISconnect' )
        return True

    def on_url_clicked( self, widget, url, event ):
        if event.button != 3:
            return False
        m = re.match( 'base:(.*)', url )
        if m:
            task_id = m.groups()[0]
            # a default URL of '(none)' is set in config.get_graph()
            # => this graph element has not been overridden by a task
            # that actually exists in the suite at the moment.
            warning_dialog( 
                    task_id + "\n"
                    "This task is part of the base graph, taken from the\n"
                    "suite config file (suite.rc) dependencies section, \n" 
                    "but it does not currently exist in the running suite." ).warn()
        else:
            # URL is task ID
            self.right_click_menu( event, url )

    def update_graph( self ):
        if self.livegraph:
            print self.livegraph.to_string()
            self.graphw.set_dotcode( self.livegraph, 'foo' )

    def visible_cb(self, model, iter ):
        # visibility determined by state matching active toggle buttons
        # set visible if model value NOT in filter_states
        state = model.get_value(iter, 1 ) 
        # strip formatting tags
        if state:
            state = re.sub( r'<.*?>', '', state )
            sres = state not in self.tfilter_states
            # AND if taskname matches filter entry text
            if self.tfilt == '':
                nres = True
            else:
                tname = model.get_value(iter, 0)
                tname = re.sub( r'<.*?>', '', tname )
                if re.search( self.tfilt, tname ):
                    nres = True
                else:
                    nres = False
        else:
            # this must be a cycle-time line (not state etc.)
            sres = True
            nres = True
        return sres and nres

    def check_tfilter_buttons(self, tb):
        del self.tfilter_states[:]
        for b in self.tfilterbox.get_children():
            if not b.get_active():
                # sub '_' from button label keyboard mnemonics
                self.tfilter_states.append( re.sub('_', '', b.get_label()))
        self.tmodelfilter.refilter()

    def check_filter_entry( self, e ):
        ftxt = self.filter_entry.get_text()
        if ftxt != '(task name filter)':
            self.tfilt = self.filter_entry.get_text()
        self.tmodelfilter.refilter()

    # close the window and quit
    def delete_event(self, widget, event, data=None):
        self.t.quit = True
        for q in self.quitters:
            #print "calling quit on ", q
            q.quit()
        #print "BYE from main thread"
        return False

    def pause_suite( self, bt ):
        try:
            god = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
            result = god.hold()
        except SuiteIdentificationError, x:
            warning_dialog( x.__str__() ).warn()
        else:
            if result.success:
                info_dialog( result.reason ).inform()
            else:
                warning_dialog( result.reason ).warn()

    def resume_suite( self, bt ):
        try:
            god = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
            result = god.resume()
        except SuiteIdentificationError, x:
            warning_dialog( x.__str__() ).warn()
        else:
            if result.success:
                info_dialog( result.reason ).inform()
            else:
                warning_dialog( result.reason ).warn()

    def stopsuite( self, bt, window, stop_rb, stopat_rb, stopnow_rb, stoptime_entry ):
        stop = False
        stopat = False
        stopnow = False
        if stop_rb.get_active():
            stop = True
        elif stopat_rb.get_active():
            stopat = True
            stoptime = stoptime_entry.get_text()
            if stoptime == '':
                warning_dialog( "No stop time entered" ).warn()
                return
            if not is_valid( stoptime ):
                warning_dialog( "Invalid stop time: " + stoptime ).warn()
                return
        elif stopnow_rb.get_active():
            stopnow = True

        window.destroy()

        try:
            god = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
            if stop:
                result = god.shutdown()
            elif stopat:
                result = god.set_stop_time( stoptime )
            elif stopnow:
                result = god.shutdown_now()
        except SuiteIdentificationError, x:
            warning_dialog( x.__str__() ).warn()
        else:
            if result.success:
                info_dialog( result.reason ).inform()
            else:
                warning_dialog( result.reason ).warn()

    def startsuite( self, bt, window, 
            coldstart_rb, warmstart_rb, rawstart_rb, restart_rb,
            entry_ctime, stoptime_entry, no_reset_cb, statedump_entry,
            optgroups ):

        command = 'cylc control run --gcylc'
        options = ''
        method = ''
        if coldstart_rb.get_active():
            method = 'coldstart'
            pass
        elif warmstart_rb.get_active():
            method = 'warmstart'
            options += ' -w'
        elif rawstart_rb.get_active():
            method = 'rawstart'
            options += ' -r'
        elif restart_rb.get_active():
            method = 'restart'
            command = 'cylc control restart --gcylc'
            if no_reset_cb.get_active():
                options += ' --no-reset'

        command += ' ' + options + ' '

        if stoptime_entry.get_text():
            command += ' --until=' + stoptime_entry.get_text()

        ctime = entry_ctime.get_text()

        if method != 'restart':
            if ctime == '':
                warning_dialog( 'Error: an initial cycle time is required' ).warn()
                return False

        for group in optgroups:
            command += group.get_options()
        window.destroy()

        command += ' ' + self.suite + ' ' + ctime
        if restart_rb.get_active():
            if statedump_entry.get_text():
                command += ' ' + statedump_entry.get_text()
        try:
            subprocess.Popen( [command], shell=True )
        except OSError, e:
            warning_dialog( 'Error: failed to start ' + self.suite ).warn()
            success = False

    def unblock_suite( self, bt ):
        try:
            god = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
            god.unblock()
        except Pyro.errors.NamingError:
            warning_dialog( 'Error: suite ' + self.suite + ' is not running' ).warn()

    def block_suite( self, bt ):
        try:
            god = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
            god.block()
        except Pyro.errors.NamingError:
            warning_dialog( 'Error: suite ' + self.suite + ' is not running' ).warn()

    def about( self, bt ):
        about = gtk.AboutDialog()
        if gtk.gtk_version[0] ==2:
            if gtk.gtk_version[1] >= 12:
                # set_program_name() was added in PyGTK 2.12
                about.set_program_name( "cylc" )
        cylc_version = 'THIS IS NOT A VERSIONED RELEASE'
        about.set_version( cylc_version )
        about.set_copyright( "(c) Hilary Oliver, NIWA, 2008-2010" )
        about.set_comments( 
"""
The cylc forecast suite metascheduler.
""" )
        about.set_website( "http://www.niwa.co.nz" )
        about.set_logo( gtk.gdk.pixbuf_new_from_file( self.imagedir + "/dew.jpg" ))
        about.run()
        about.destroy()

    def click_exit( self, foo ):
        self.t.quit = True
        for q in self.quitters:
            #print "calling quit on ", q
            q.quit()

        #print "BYE from main thread"
        self.window.destroy()
        return False

    def toggle_headings( self, w ):
        if self.task_headings_on:
            self.no_task_headings()
        else:
            self.full_task_headings()

    def no_task_headings( self ):
        self.task_headings_on = False
        self.led_headings = ['Cycle Time' ] + [''] * len( self.task_list )
        self.reset_led_headings()

    def full_task_headings( self ):
        self.task_headings_on = True
        self.led_headings = ['Cycle Time' ] + self.task_list
        self.reset_led_headings()

    def reset_led_headings( self ):
        tvcs = self.led_treeview.get_columns()
        for n in range( 1,1+len( self.task_list) ):
            heading = self.led_headings[n]
            # double on underscores or they get turned into underlines
            # (may be related to keyboard mnemonics for button labels?)
            heading = re.sub( '_', '__', heading )
            tvcs[n].set_title( heading )

    def ledview_widgets( self ):
        types = tuple( [gtk.gdk.Pixbuf]* (10 + len( self.task_list)))
        liststore = gtk.ListStore( *types )
        treeview = gtk.TreeView( liststore )
        treeview.get_selection().set_mode( gtk.SELECTION_NONE )

        # this is how to set background color of the entire treeview to black:
        #treeview.modify_base( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#000' ) ) 

        tvc = gtk.TreeViewColumn( 'Cycle Time' )
        for i in range(10):
            cr = gtk.CellRendererPixbuf()
            #cr.set_property( 'cell-background', 'black' )
            tvc.pack_start( cr, False )
            tvc.set_attributes( cr, pixbuf=i )
        treeview.append_column( tvc )

        # hardwired 10px lamp image width!
        lamp_width = 10

        for n in range( 10, 10+len( self.task_list )):
            cr = gtk.CellRendererPixbuf()
            #cr.set_property( 'cell_background', 'black' )
            cr.set_property( 'xalign', 0 )
            tvc = gtk.TreeViewColumn( ""  )
            tvc.set_min_width( lamp_width )  # WIDTH OF LED PIXBUFS
            tvc.pack_end( cr, True )
            tvc.set_attributes( cr, pixbuf=n )
            treeview.append_column( tvc )

        sw = gtk.ScrolledWindow()
        sw.set_policy( gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC )

        self.led_treeview = treeview
        sw.add( treeview )
        return sw
    
    def treeview_widgets( self ):
        # Treeview of current suite state, with filtering and sorting.
        # sorting is handled somewhat manually because the simple method 
        # of interposing a TreeModelSort at the top:
        #   treestore = gtk.TreeStore(str, ...)
        #   tms = gtk.TreeModelSort( treestore )   #\ 
        #   tmf = tms.filter_new()                 #-- or other way round?
        #   tv = gtk.TreeView()
        #   tv.set_model(tms)
        # failed to produce correct results (the data displayed was not 
        # consistently what should have been displayed given the
        # filtering in use) although the exact same code worked for a
        # liststore.

        self.ttreestore = gtk.TreeStore(str, str, str, str, str, str, str )
        self.tmodelfilter = self.ttreestore.filter_new()
        self.tmodelfilter.set_visible_func(self.visible_cb)
        self.ttreeview = gtk.TreeView()
        self.ttreeview.set_model(self.tmodelfilter)

        ts = self.ttreeview.get_selection()
        ts.set_mode( gtk.SELECTION_SINGLE )

        self.ttreeview.connect( 'button_press_event', self.on_treeview_button_pressed )

        headings = ['task', 'state', 'message', 'Tsubmit', 'Tstart', 'mean dT', 'ETC' ]
        bkgcols  = [ None,  '#def',  '#fff',    '#def',    '#fff',   '#def',    '#fff']
        for n in range(len(headings)):
            cr = gtk.CellRendererText()
            cr.set_property( 'cell-background', bkgcols[n] )
            #tvc = gtk.TreeViewColumn( headings[n], cr, text=n )
            tvc = gtk.TreeViewColumn( headings[n], cr, markup=n )
            tvc.set_resizable(True)
            if n == 0:
                # allow click sorting only on first column (cycle time
                # and task name) as I don't understand the effect of
                # sorting on other columns in a treeview (it doesn't
                # seem to work as expected).
                tvc.set_clickable(True)
                tvc.connect("clicked", self.rearrange, n )
                tvc.set_sort_order(gtk.SORT_ASCENDING)
                tvc.set_sort_indicator(True)
                self.ttreestore.set_sort_column_id(n, gtk.SORT_ASCENDING ) 
            self.ttreeview.append_column(tvc)
 
        sw = gtk.ScrolledWindow()
        sw.set_policy( gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC )
        sw.add( self.ttreeview )

        self.tfilterbox = gtk.HBox()

        # allow filtering out of 'finished' and 'waiting'
        all_states = [ 'waiting', 'submitted', 'running', 'finished', 'failed' ]
        labels = {}
        labels[ 'waiting'   ] = '_waiting'
        labels[ 'submitted' ] = 's_ubmitted'
        labels[ 'running'   ] = '_running'
        labels[ 'finished'  ] = 'f_inished'
        labels[ 'failed'    ] = 'f_ailed'
 
        # initially filter out 'finished' and 'waiting' tasks
        self.tfilter_states = [ 'waiting', 'finished' ]

        for st in all_states:
            b = gtk.CheckButton( labels[st] )
            self.tfilterbox.pack_start(b)
            if st in self.tfilter_states:
                b.set_active(False)
            else:
                b.set_active(True)
            b.connect('toggled', self.check_tfilter_buttons)

        hbox = gtk.HBox()
        eb = gtk.EventBox()
        eb.add( gtk.Label( "BELOW: right-click on tasks to control or interrogate" ) )
        eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#8be' ) ) 
        hbox.pack_start( eb, True )

        bbox = gtk.HButtonBox()
        expand_button = gtk.Button( "E_xpand" )
        expand_button.connect( 'clicked', lambda x: self.ttreeview.expand_all() )
        collapse_button = gtk.Button( "_Collapse" )
        collapse_button.connect( 'clicked', lambda x: self.ttreeview.collapse_all() )
     
        bbox.add( expand_button )
        bbox.add( collapse_button )
        bbox.set_layout( gtk.BUTTONBOX_START )

        self.filter_entry = gtk.Entry()
        self.filter_entry.set_text( '(task name filter)' )
        self.filter_entry.connect( "activate", self.check_filter_entry )

        ahbox = gtk.HBox()
        ahbox.pack_start( bbox, True )
        ahbox.pack_start( self.filter_entry, True )
        ahbox.pack_start( self.tfilterbox, True)

        vbox = gtk.VBox()
        vbox.pack_start( hbox, False )
        vbox.pack_start( sw, True )
        vbox.pack_end( ahbox, False )

        return vbox

    def view_task_info( self, w, task_id, jsonly ):
        [ glbl, states ] = self.get_pyro( 'state_summary').get_state_summary()
        view = True
        reasons = []
        try:
            logfiles = states[ task_id ][ 'logfiles' ]
        except KeyError:
            warning_dialog( task_id + ' is no longer live' ).warn()
            return False

        if len(logfiles) == 0:
            view = False
            reasons.append( task_id + ' has no associated log files' )

        if states[ task_id ][ 'state' ] == 'waiting':
            view = False
            reasons.append( task_id + ' has not started yet' )

        if not view:
            warning_dialog( '\n'.join( reasons ) ).warn()
        else:
            self.popup_logview( task_id, logfiles, jsonly )

        return False

    def on_treeview_button_pressed( self, treeview, event ):
        # DISPLAY MENU ONLY ON RIGHT CLICK ONLY
        if event.button != 3:
            return False

        # the following sets selection to the position at which the
        # right click was done (otherwise selection lags behind the
        # right click):
        x = int( event.x )
        y = int( event.y )
        time = event.time
        pth = treeview.get_path_at_pos(x,y)

        if pth is None:
            return False

        treeview.grab_focus()
        path, col, cellx, celly = pth
        treeview.set_cursor( path, col, 0 )

        selection = treeview.get_selection()
        treemodel, iter = selection.get_selected()
        name = treemodel.get_value( iter, 0 )
        iter2 = treemodel.iter_parent( iter )
        try:
            ctime = treemodel.get_value( iter2, 0 )
        except TypeError:
            # must have clicked on the top level ctime 
            return

        task_id = name + '%' + ctime

        self.right_click_menu( event, task_id )

    def right_click_menu( self, event, task_id ):
        menu = gtk.Menu()

        menu_root = gtk.MenuItem( task_id )
        menu_root.set_submenu( menu )

        title_item = gtk.MenuItem( 'Task: ' + task_id )
        title_item.set_sensitive(False)
        menu.append( title_item )

        menu.append( gtk.SeparatorMenuItem() )

        js_item = gtk.MenuItem( 'View Job Script' )
        menu.append( js_item )
        js_item.connect( 'activate', self.view_task_info, task_id, True )

        info_item = gtk.MenuItem( 'View Job Stdout & Stderr' )
        menu.append( info_item )
        info_item.connect( 'activate', self.view_task_info, task_id, False )

        info_item = gtk.MenuItem( 'View Task Prerequisites & Outputs' )
        menu.append( info_item )
        info_item.connect( 'activate', self.popup_requisites, task_id )

        menu.append( gtk.SeparatorMenuItem() )

        reset_ready_item = gtk.MenuItem( 'Trigger Task' )
        menu.append( reset_ready_item )
        reset_ready_item.connect( 'activate', self.reset_task_state, task_id, 'ready' )
        if self.readonly:
            reset_ready_item.set_sensitive(False)

        reset_waiting_item = gtk.MenuItem( 'Reset State to "waiting"' )
        menu.append( reset_waiting_item )
        reset_waiting_item.connect( 'activate', self.reset_task_state, task_id, 'waiting' )
        if self.readonly:
            reset_waiting_item.set_sensitive(False)

        reset_finished_item = gtk.MenuItem( 'Reset State to "finished"' )
        menu.append( reset_finished_item )
        reset_finished_item.connect( 'activate', self.reset_task_state, task_id, 'finished' )
        if self.readonly:
            reset_finished_item.set_sensitive(False)

        reset_failed_item = gtk.MenuItem( 'Reset State to "failed"' )
        menu.append( reset_failed_item )
        reset_failed_item.connect( 'activate', self.reset_task_state, task_id, 'failed' )
        if self.readonly:
            reset_failed_item.set_sensitive(False)

        menu.append( gtk.SeparatorMenuItem() )

        kill_item = gtk.MenuItem( 'Remove Task (after spawning)' )
        menu.append( kill_item )
        kill_item.connect( 'activate', self.kill_task, task_id )
        if self.readonly:
            kill_item.set_sensitive(False)

        kill_nospawn_item = gtk.MenuItem( 'Remove Task (without spawning)' )
        menu.append( kill_nospawn_item )
        kill_nospawn_item.connect( 'activate', self.kill_task_nospawn, task_id )
        if self.readonly:
            kill_nospawn_item.set_sensitive(False)

        purge_item = gtk.MenuItem( 'Remove Task (Recursive Purge)' )
        menu.append( purge_item )
        purge_item.connect( 'activate', self.popup_purge, task_id )
        if self.readonly:
            purge_item.set_sensitive(False)

        menu.show_all()
        menu.popup( None, None, None, event.button, event.time )

        # TO DO: POPUP MENU MUST BE DESTROY()ED AFTER EVERY USE AS
        # POPPING DOWN DOES NOT DO THIS (=> MEMORY LEAK?)?????????
        return True

    def rearrange( self, col, n ):
        cols = self.ttreeview.get_columns()
        for i_n in range(0,len(cols)):
            if i_n == n: 
                cols[i_n].set_sort_indicator(True)
            else:
                cols[i_n].set_sort_indicator(False)
        # col is cols[n]
        if col.get_sort_order() == gtk.SORT_ASCENDING:
            col.set_sort_order(gtk.SORT_DESCENDING)
        else:
            col.set_sort_order(gtk.SORT_ASCENDING)
        self.ttreestore.set_sort_column_id(n, col.get_sort_order()) 

    def update_tb( self, tb, line, tags = None ):
        if tags:
            tb.insert_with_tags( tb.get_end_iter(), line, *tags )
        else:
            tb.insert( tb.get_end_iter(), line )


    def popup_requisites( self, w, task_id ):
        window = gtk.Window()
        #window.set_border_width( 10 )
        window.set_title( task_id + ": Prerequisites and Outputs" )
        #window.modify_bg( gtk.STATE_NORMAL, 
        #       gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_size_request(400, 300)

        sw = gtk.ScrolledWindow()
        sw.set_policy( gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC )

        vbox = gtk.VBox()
        quit_button = gtk.Button( "_Close" )
        quit_button.connect("clicked", lambda x: window.destroy() )
        vbox.pack_start( sw )
        vbox.pack_start( quit_button, False )

        textview = gtk.TextView()
        textview.set_border_width(5)
        textview.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( "#fff" ))
        textview.set_editable( False )
        sw.add( textview )
        window.add( vbox )
        tb = textview.get_buffer()

        blue = tb.create_tag( None, foreground = "blue" )
        red = tb.create_tag( None, foreground = "red" )
        bold = tb.create_tag( None, weight = pango.WEIGHT_BOLD )
 
        result = self.get_pyro( 'remote' ).get_task_requisites( [ task_id ] )
        if result:
            # (else no tasks were found at all -suite shutting down)
            if task_id not in result:
                warning_dialog( 
                    "Task proxy " + task_id + " not found in " + self.suite + \
                 ".\nTasks are removed once they are no longer needed.").warn()
                return
        
        #self.update_tb( tb, 'Task ' + task_id + ' in ' +  self.suite + '\n\n', [bold])
        self.update_tb( tb, 'TASK ', [bold] )
        self.update_tb( tb, task_id, [bold, blue])
        self.update_tb( tb, ' in SUITE ', [bold] )
        self.update_tb( tb, self.suite + '\n\n', [bold, blue])

        [ pre, out, extra_info ] = result[ task_id ]

        self.update_tb( tb, 'Prerequisites', [bold])
        #self.update_tb( tb, ' blue => satisfied,', [blue] )
        self.update_tb( tb, ' (' )
        self.update_tb( tb, 'red', [red] )
        self.update_tb( tb, '=> NOT satisfied)\n') 

        if len( pre ) == 0:
            self.update_tb( tb, ' - (None)\n' )
        for item in pre:
            [ msg, state ] = item
            if state:
                tags = None
            else:
                tags = [red]
            self.update_tb( tb, ' - ' + msg + '\n', tags )

        self.update_tb( tb, '\nOutputs', [bold] )
        self.update_tb( tb, ' (' )
        self.update_tb( tb, 'red', [red] )
        self.update_tb( tb, '=> NOT completed)\n') 

        if len( out ) == 0:
            self.update_tb( tb, ' - (None)\n')
        for item in out:
            [ msg, state ] = item
            if state:
                tags = []
            else:
                tags = [red]
            self.update_tb( tb, ' - ' + msg + '\n', tags )

        if len( extra_info.keys() ) > 0:
            self.update_tb( tb, '\nOther\n', [bold] )
            for item in extra_info:
                self.update_tb( tb, ' - ' + item + ': ' + str( extra_info[ item ] ) + '\n' )

        #window.connect("delete_event", lv.quit_w_e)
        window.show_all()

    def on_popup_quit( self, b, lv, w ):
        lv.quit()
        self.quitters.remove( lv )
        w.destroy()

    def reset_task_state( self, b, task_id, state ):
        msg = "reset " + task_id + " to " + state +"?"
        prompt = gtk.MessageDialog( None, gtk.DIALOG_MODAL, gtk.MESSAGE_QUESTION, gtk.BUTTONS_OK_CANCEL, msg )
        response = prompt.run()
        prompt.destroy()
        if response != gtk.RESPONSE_OK:
            return
        try:
            proxy = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port).get_proxy( 'remote' )
        except SuiteIdentificationError, x:
            # the suite was probably shut down by another process
            warning_dialog( x.__str__() ).warn()
            return
        result = proxy.reset_task_state( task_id, state )
        if result.success:
            info_dialog( result.reason ).inform()
        else:
            warning_dialog( result.reason ).warn()

    def kill_task( self, b, task_id ):
        msg = "remove " + task_id + " (after spawning)?"
        prompt = gtk.MessageDialog( None, gtk.DIALOG_MODAL, gtk.MESSAGE_QUESTION, gtk.BUTTONS_OK_CANCEL, msg )
        response = prompt.run()
        prompt.destroy()
        if response != gtk.RESPONSE_OK:
            return
        proxy = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port).get_proxy( 'remote' )
        actioned, explanation = proxy.spawn_and_die( task_id )
 
    def kill_task_nospawn( self, b, task_id ):
        msg = "remove " + task_id + " (without spawning)?"
        prompt = gtk.MessageDialog( None, gtk.DIALOG_MODAL, gtk.MESSAGE_QUESTION, gtk.BUTTONS_OK_CANCEL, msg )
        response = prompt.run()
        prompt.destroy()
        if response != gtk.RESPONSE_OK:
            return
        proxy = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port).get_proxy( 'remote' )
        actioned, explanation = proxy.die( task_id )

    def purge_cycle_entry( self, e, w, task_id ):
        stop = e.get_text()
        w.destroy()
        proxy = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
        actioned, explanation = proxy.purge( task_id, stop )

    def purge_cycle_button( self, b, e, w, task_id ):
        stop = e.get_text()
        w.destroy()
        proxy = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
        actioned, explanation = proxy.purge( task_id, stop )

    def stopsuite_popup( self, b ):
        window = gtk.Window()
        window.modify_bg( gtk.STATE_NORMAL, 
                gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_border_width(5)
        window.set_title( "Stop Suite '" + self.suite + "'")

        vbox = gtk.VBox()

        box = gtk.HBox()
        stop_rb = gtk.RadioButton( None, "Stop" )
        box.pack_start (stop_rb, True)
        stopat_rb = gtk.RadioButton( stop_rb, "Stop At" )
        box.pack_start (stopat_rb, True)
        stopnow_rb = gtk.RadioButton( stop_rb, "Stop NOW" )
        box.pack_start (stopnow_rb, True)
        stop_rb.set_active(True)
        vbox.pack_start( box )

        box = gtk.HBox()
        label = gtk.Label( 'Stop At (YYYYMMDDHH)' )
        box.pack_start( label, True )
        stoptime_entry = gtk.Entry()
        stoptime_entry.set_max_length(10)
        stoptime_entry.set_sensitive(False)
        box.pack_start (stoptime_entry, True)
        vbox.pack_start( box )

        stop_rb.connect( "toggled", self.stop_method, "stop", stoptime_entry )
        stopat_rb.connect( "toggled", self.stop_method, "stopat", stoptime_entry )
        stopnow_rb.connect(   "toggled", self.stop_method, "stopnow", stoptime_entry )

        cancel_button = gtk.Button( "_Cancel" )
        cancel_button.connect("clicked", lambda x: window.destroy() )

        stop_button = gtk.Button( "_Stop" )
        stop_button.connect("clicked", self.stopsuite, 
                window, stop_rb, stopat_rb, stopnow_rb,
                stoptime_entry )

        help_button = gtk.Button( "_Help" )
        help_button.connect("clicked", helpwindow.stop_guide )

        hbox = gtk.HBox()
        hbox.pack_start( stop_button, False )
        hbox.pack_end( cancel_button, False )
        hbox.pack_end( help_button, False )
        vbox.pack_start( hbox )

        window.add( vbox )
        window.show_all()

    def stop_method( self, b, meth, stoptime_entry ):
        if meth == 'stop' or meth == 'stopnow':
            stoptime_entry.set_sensitive( False )
        else:
            stoptime_entry.set_sensitive( True )

    def startup_method( self, b, meth, ctime_entry, statedump_entry, no_reset_cb ):
        if meth == 'cold' or meth == 'warm' or meth == 'raw':
            statedump_entry.set_sensitive( False )
            ctime_entry.set_sensitive( True )
        else:
            # restart
            statedump_entry.set_sensitive( True )
            ctime_entry.set_sensitive( False )
            no_reset_cb.set_sensitive(True)

    def startsuite_popup( self, b ):
        window = gtk.Window()
        window.modify_bg( gtk.STATE_NORMAL, 
                gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_border_width(5)
        window.set_title( "Start Suite '" + self.suite + "'")

        vbox = gtk.VBox()

        box = gtk.HBox()
        coldstart_rb = gtk.RadioButton( None, "Cold Start" )
        box.pack_start (coldstart_rb, True)
        warmstart_rb = gtk.RadioButton( coldstart_rb, "Warm Start" )
        box.pack_start (warmstart_rb, True)
        rawstart_rb = gtk.RadioButton( coldstart_rb, "Raw Start" )
        box.pack_start (rawstart_rb, True)
        restart_rb = gtk.RadioButton( coldstart_rb, "Restart" )
        box.pack_start (restart_rb, True)
        coldstart_rb.set_active(True)
        vbox.pack_start( box )

        box = gtk.HBox()
        label = gtk.Label( 'Start (YYYYMMDDHH)' )
        box.pack_start( label, True )
        ctime_entry = gtk.Entry()
        ctime_entry.set_max_length(10)
        #ctime_entry.set_width_chars(10)
        box.pack_start (ctime_entry, True)
        vbox.pack_start( box )

        box = gtk.HBox()
        label = gtk.Label( 'Stop (YYYYMMDDHH)' )
        box.pack_start( label, True )
        stoptime_entry = gtk.Entry()
        stoptime_entry.set_max_length(10)
        #stoptime_entry.set_width_chars(10)
        box.pack_start (stoptime_entry, True)
        vbox.pack_start( box )

        box = gtk.HBox()
        label = gtk.Label( 'Initial State (FILE)' )
        box.pack_start( label, True )
        statedump_entry = gtk.Entry()
        statedump_entry.set_text( 'state' )
        statedump_entry.set_sensitive( False )
        box.pack_start (statedump_entry, True)
        vbox.pack_start(box)

        no_reset_cb = gtk.CheckButton( "Don't reset failed tasks" )
        no_reset_cb.set_active(False)
        no_reset_cb.set_sensitive(False)
        vbox.pack_start (no_reset_cb, True)

        coldstart_rb.connect( "toggled", self.startup_method, "cold", ctime_entry, statedump_entry, no_reset_cb )
        warmstart_rb.connect( "toggled", self.startup_method, "warm", ctime_entry, statedump_entry, no_reset_cb )
        rawstart_rb.connect ( "toggled", self.startup_method, "raw",  ctime_entry, statedump_entry, no_reset_cb )
        restart_rb.connect(   "toggled", self.startup_method, "re",   ctime_entry, statedump_entry, no_reset_cb )

        dmode_group = controlled_option_group( "Dummy Mode", "--dummy-mode" )
        dmode_group.add_entry( 
                'Fail Task (NAME%YYYYMMDDHH)',
                '--fail='
                )
        dmode_group.pack( vbox )
        
        stpaused_group = controlled_option_group( "Pause Immediately", "--paused" )
        stpaused_group.pack( vbox )

        debug_group = controlled_option_group( "Debug Mode", "--debug" )
        debug_group.pack( vbox )

        optgroups = [ dmode_group, debug_group, stpaused_group ]

        cancel_button = gtk.Button( "_Cancel" )
        cancel_button.connect("clicked", lambda x: window.destroy() )

        start_button = gtk.Button( "_Start" )
        start_button.connect("clicked", self.startsuite, 
                window, coldstart_rb, warmstart_rb, rawstart_rb, restart_rb,
                ctime_entry, stoptime_entry, no_reset_cb, 
                statedump_entry, optgroups )

        help_button = gtk.Button( "_Help" )
        help_button.connect("clicked", helpwindow.start_guide )

        hbox = gtk.HBox()
        hbox.pack_start( start_button, False )
        hbox.pack_end( cancel_button, False )
        hbox.pack_end( help_button, False )
        vbox.pack_start( hbox )

        window.add( vbox )
        window.show_all()

    def popup_purge( self, b, task_id ):
        window = gtk.Window()
        window.modify_bg( gtk.STATE_NORMAL, 
                gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_border_width(5)
        window.set_title( "Purge " + task_id )
        #window.set_size_request(800, 300)

        sw = gtk.ScrolledWindow()
        sw.set_policy( gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC )

        vbox = gtk.VBox()
        label = gtk.Label( 'stop cycle (inclusive)' )

        entry = gtk.Entry()
        entry.set_max_length(10)
        entry.connect( "activate", self.purge_cycle_entry, window, task_id )

        hbox = gtk.HBox()
        hbox.pack_start( label, True )
        hbox.pack_start (entry, True)
        vbox.pack_start( hbox )

        cancel_button = gtk.Button( "_Cancel" )
        cancel_button.connect("clicked", lambda x: window.destroy() )

        start_button = gtk.Button( "_Purge" )
        start_button.connect("clicked", self.purge_cycle_button, entry, window, task_id )

        hbox = gtk.HBox()
        hbox.pack_start( cancel_button, True )
        hbox.pack_start(start_button, True)
        vbox.pack_start( hbox )

        # TO DO:
        #help_button = gtk.Button( "Help" )
        #help_button.connect("clicked", self.purge_guide )

        window.add( vbox )
        window.show_all()

    def ctime_entry_popup( self, b, callback, title ):
        window = gtk.Window()
        window.modify_bg( gtk.STATE_NORMAL, 
                gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_border_width(5)
        window.set_title( title )
        #window.set_size_request(800, 300)

        sw = gtk.ScrolledWindow()
        sw.set_policy( gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC )

        vbox = gtk.VBox()

        hbox = gtk.HBox()
        label = gtk.Label( 'Cycle Time' )
        hbox.pack_start( label, True )
        entry_ctime = gtk.Entry()
        entry_ctime.set_max_length(10)
        hbox.pack_start (entry_ctime, True)
        vbox.pack_start(hbox)

        go_button = gtk.Button( "Go" )
        go_button.connect("clicked", callback, window, entry_ctime )
        vbox.pack_start(go_button)
 
        window.add( vbox )
        window.show_all()

    def insert_task_popup( self, b ):
        window = gtk.Window()
        window.modify_bg( gtk.STATE_NORMAL, 
                gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_border_width(5)
        window.set_title( "Insert a task or task group" )
        #window.set_size_request(800, 300)

        sw = gtk.ScrolledWindow()
        sw.set_policy( gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC )

        vbox = gtk.VBox()

        hbox = gtk.HBox()
        label = gtk.Label( 'Task or Insertion Group name' )
        hbox.pack_start( label, True )
        entry_name = gtk.Entry()
        hbox.pack_start (entry_name, True)
        vbox.pack_start(hbox)

        hbox = gtk.HBox()
        label = gtk.Label( 'Cycle Time' )
        hbox.pack_start( label, True )
        entry_ctime = gtk.Entry()
        entry_ctime.set_max_length(10)
        hbox.pack_start (entry_ctime, True)
        vbox.pack_start(hbox)

        help_button = gtk.Button( "_Help" )
        help_button.connect("clicked", helpwindow.insertion )

        hbox = gtk.HBox()
        insert_button = gtk.Button( "_Insert" )
        insert_button.connect("clicked", self.insert_task, window, entry_name, entry_ctime )
        cancel_button = gtk.Button( "_Cancel" )
        cancel_button.connect("clicked", lambda x: window.destroy() )
        hbox.pack_start(insert_button, False)
        hbox.pack_end(cancel_button, False)
        hbox.pack_end(help_button, False)
        vbox.pack_start( hbox )

        window.add( vbox )
        window.show_all()

    def insert_task( self, w, window, entry_name, entry_ctime ):
        name = entry_name.get_text()
        ctime = entry_ctime.get_text()
        if not is_valid( ctime ):
            warning_dialog( "Cycle time not valid: " + ctime).warn()
            return
        if name == '':
            warning_dialog( "Enter task or group name" ).warn()
            return
        window.destroy()
        task_id = name + '%' + ctime
        proxy = cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( 'remote' )
        try:
            result = proxy.insert( task_id )
        except SuiteIdentificationError, x:
            warning_dialog( x.__str__() ).warn()
        else:
            if result.success:
                info_dialog( result.reason ).inform()
            else:
                warning_dialog( result.reason ).warn()

    def nudge_suite( self, w ):
        try:
            proxy = cylc_pyro_client.client( self.suite ).get_proxy( 'remote' )
        except SuiteIdentificationError, x:
            warning_dialog( str(x) ).warn()
            return False
        result = proxy.nudge()
        if not result:
            warning_dialog( 'Failed to nudge the suite' ).warn()

    def popup_logview( self, task_id, logfiles, jsonly ):
        # TO DO: jsonly is dirty hack to separate the job script from
        # task log files; we should do this properly by storing them
        # separately in the task proxy, or at least separating them in
        # the suite state summary.
        window = gtk.Window()
        window.modify_bg( gtk.STATE_NORMAL, 
                gtk.gdk.color_parse( self.log_colors.get_color()))
        window.set_border_width(5)
        logs = []
        for f in logfiles:
            if re.search( 'cylc-', f ):
                js = f
            else:
                logs.append(f)

        window.set_size_request(800, 300)
        if jsonly:
            window.set_title( task_id + ": Task Job Submission Script" )
            lv = textload( task_id, js )
        else:
            # put '.out' before '.err'
            logs.sort( reverse=True )
            window.set_title( task_id + ": Task Logs" )
            lv = combo_logviewer( task_id, logs )
        #print "ADDING to quitters: ", lv
        self.quitters.append( lv )

        window.add( lv.get_widget() )

        #state_button = gtk.Button( "Interrogate" )
        #state_button.connect("clicked", self.popup_requisites, task_id )
 
        quit_button = gtk.Button( "_Close" )
        quit_button.connect("clicked", self.on_popup_quit, lv, window )
        
        lv.hbox.pack_start( quit_button, False )
        #lv.hbox.pack_start( state_button )

        window.connect("delete_event", lv.quit_w_e)
        window.show_all()


    def create_menu( self ):
        file_menu = gtk.Menu()

        file_menu_root = gtk.MenuItem( '_File' )
        file_menu_root.set_submenu( file_menu )

        exit_item = gtk.MenuItem( 'E_xit (Disconnect From Suite)' )
        exit_item.connect( 'activate', self.click_exit )
        file_menu.append( exit_item )

        view_menu = gtk.Menu()
        view_menu_root = gtk.MenuItem( '_View' )
        view_menu_root.set_submenu( view_menu )

        names_item = gtk.MenuItem( '_Toggle Task Names (light panel)' )
        view_menu.append( names_item )
        names_item.connect( 'activate', self.toggle_headings )

        nudge_item = gtk.MenuItem( "_Nudge Suite (update times)" )
        view_menu.append( nudge_item )
        nudge_item.connect( 'activate', self.nudge_suite  )

        log_item = gtk.MenuItem( 'View _Suite Log' )
        view_menu.append( log_item )
        log_item.connect( 'activate', self.view_log )

        start_menu = gtk.Menu()
        start_menu_root = gtk.MenuItem( 'Suite _Control' )
        start_menu_root.set_submenu( start_menu )

        start_item = gtk.MenuItem( '_Run (cold-, warm-, raw-, re-start)' )
        start_menu.append( start_item )
        start_item.connect( 'activate', self.startsuite_popup )
        if self.readonly:
            start_item.set_sensitive(False)

        stop_item = gtk.MenuItem( '_Stop (soon, now, or later)' )
        start_menu.append( stop_item )
        stop_item.connect( 'activate', self.stopsuite_popup )
        if self.readonly:
            stop_item.set_sensitive(False)

        pause_item = gtk.MenuItem( '_Pause (stop submitting tasks)' )
        start_menu.append( pause_item )
        pause_item.connect( 'activate', self.pause_suite )
        if self.readonly:
            pause_item.set_sensitive(False)

        resume_item = gtk.MenuItem( '_Unpause (resume submitting tasks)' )
        start_menu.append( resume_item )
        resume_item.connect( 'activate', self.resume_suite )
        if self.readonly:
            resume_item.set_sensitive(False)

        insert_item = gtk.MenuItem( '_Insert a Task or Group' )
        start_menu.append( insert_item )
        insert_item.connect( 'activate', self.insert_task_popup )
        if self.readonly:
            insert_item.set_sensitive(False)

        block_item = gtk.MenuItem( '_Block (ignore intervention requests)' )
        start_menu.append( block_item )
        block_item.connect( 'activate', self.block_suite )
        if self.readonly or not self.use_block:
            block_item.set_sensitive(False)

        unblock_item = gtk.MenuItem( 'U_nblock (comply with intervention requests)' )
        start_menu.append( unblock_item )
        unblock_item.connect( 'activate', self.unblock_suite )
        if self.readonly or not self.use_block:
            unblock_item.set_sensitive(False)

        help_menu = gtk.Menu()
        help_menu_root = gtk.MenuItem( '_Help' )
        help_menu_root.set_submenu( help_menu )

        guide_item = gtk.MenuItem( '_Quick Guide' )
        help_menu.append( guide_item )
        guide_item.connect( 'activate', helpwindow.userguide )
 
        about_item = gtk.MenuItem( '_About' )
        help_menu.append( about_item )
        about_item.connect( 'activate', self.about )
      
        self.menu_bar = gtk.MenuBar()
        self.menu_bar.append( file_menu_root )
        self.menu_bar.append( view_menu_root )
        self.menu_bar.append( start_menu_root )
        self.menu_bar.append( help_menu_root )

    def create_info_bar( self ):
        self.label_status = gtk.Label( "status..." )
        self.label_mode = gtk.Label( "mode..." )
        self.label_time = gtk.Label( "time..." )
        self.label_suitename = gtk.Label( self.suite )

        hbox = gtk.HBox()

        eb = gtk.EventBox()
        eb.add( self.label_suitename )
        #eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#ed9638' ) )
        eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#88bbee' ) )
        hbox.pack_start( eb, True )

        eb = gtk.EventBox()
        eb.add( self.label_mode )
        #eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#dbd40a' ) )
        eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#bbddff' ) )
        hbox.pack_start( eb, True )

        eb = gtk.EventBox()
        eb.add( self.label_status )
        #eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#a7c339' ) )
        eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#88bbee' ) )
        hbox.pack_start( eb, True )

        eb = gtk.EventBox()
        eb.add( self.label_time )
        #eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#6ab7b4' ) ) 
        #eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#fa87a4' ) ) 
        eb.modify_bg( gtk.STATE_NORMAL, gtk.gdk.color_parse( '#bbddff' ) ) 
        hbox.pack_start( eb, True )

        return hbox

    #def check_connection( self ):
    #    # called on a timeout in the gtk main loop, tell the log viewer
    #    # to reload if the connection has been lost and re-established,
    #    # which probably means the cylc suite was shutdown and
    #    # restarted.
    #    try:
    #        cylc_pyro_client.ping( self.host, self.port )
    #    except Pyro.errors.ProtocolError:
    #        print "NO CONNECTION"
    #        self.connection_lost = True
    #    else:
    #        print "CONNECTED"
    #        if self.connection_lost:
    #            #print "------>INITIAL RECON"
    #            self.connection_lost = False
    #    # always return True so that we keep getting called
    #    return True

    def get_pyro( self, object ):
        return cylc_pyro_client.client( self.suite, self.owner, self.host, self.port ).get_proxy( object )
 
    def preload_task_list( self ):
        # Load task list from suite config.
        ### TO DO: For suites that are already running, or for dynamically
        ### updating the viewed task list, we can retrieve the task list
        ### (etc.) from the suite's remote state summary object.
        suiterc = config( self.suite )
        self.task_list = suiterc.get_full_task_name_list()
        self.use_block = suiterc['use suite blocking']

    def view_log( self, w ):
        suiterc = config( self.suite )
        logdir = os.path.join( suiterc['top level logging directory'], self.suite )
        foo = cylc_logviewer( 'log', logdir, suiterc.get_full_task_name_list() )
        self.quitters.append(foo)

class StandaloneControlApp( ControlApp ):
    # For a ControlApp not launched by the gcylc main app: 
    # 1/ call gobject.threads_init() on startup
    # 2/ call gtk.main_quit() on exit

    def __init__(self, suite, owner, host, port, suite_dir, logging_dir, imagedir, readonly=False ):
        gobject.threads_init()
        ControlApp.__init__(self, suite, owner, host, port, suite_dir, logging_dir, imagedir, readonly )
 
    def delete_event(self, widget, event, data=None):
        ControlApp.delete_event( self, widget, event, data )
        gtk.main_quit()

    def click_exit( self, foo ):
        ControlApp.click_exit( self, foo )
        gtk.main_quit()
