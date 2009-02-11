#! /usr/bin/env python
## vim: fileencoding=utf-8
#
# Copyright (c) 2007-2009 Adeodato Simó (dato@net.com.org.es)
# Licensed under the terms of the MIT license.

import bisect

from PyQt4.QtCore import Qt
from PyQt4 import QtGui, QtCore
from PyKDE4 import kio, kdecore

import minirok
from minirok import proxy, util

DRAG_MIME_TYPE = 'text/x-minirok-track-list'

# TODO handle rename/refresh
# TODO auto open (see AUTOMATICALLY_EXPAND.diff)
# TODO fetchMore(children) when expanding parent?

##

class TreeView(QtGui.QTreeView):

    def __init__(self, parent=None):
        QtGui.QTreeView.__init__(self, parent)

        self.header().hide()
        self.setRootIsDecorated(True)
        self.setAllColumnsShowFocus(True)
        self.setExpandsOnDoubleClick(False)
        self.setDragDropMode(self.DragOnly)
        self.setSelectionBehavior(self.SelectRows)
        self.setSelectionMode(self.ExtendedSelection)

        self.connect(self, QtCore.SIGNAL('activated(const QModelIndex &)'),
                self.slot_append_selected)

    def slot_append_selected(self, index):
        # self.setExpanded(index, not self.isExpanded(index))
        minirok.Globals.playlist.add_files(
                map(util.kurl_to_path,
                    self.model().urls_from_indexes(self.selectedIndexes())))

    def startDrag(self, action):
        mimedata = self.model().mimeData(self.selectedIndexes())
        ntracks = len(mimedata.urls())

        # display a "tooltip" with the number of tracks
        text = '%d track%s' % (ntracks, ntracks > 1 and 's' or '')
        metrics = self.fontMetrics()
        width = metrics.width(text)
        height = metrics.height()
        ascent = metrics.ascent()

        self.pixmap = QtGui.QPixmap(width+4, height) # "self" needed
        self.pixmap.fill(self, 0, 0)
        painter = QtGui.QPainter(self.pixmap)
        painter.drawText(2, ascent+1, text)

        dragobj = QtGui.QDrag(self)
        dragobj.setMimeData(mimedata)
        dragobj.setPixmap(self.pixmap)

        dragobj.exec_(action)

##

class Model(QtCore.QAbstractItemModel):

    CONFIG_SECTION = 'Tree View'
    CONFIG_RECURSE_OPTION = 'RecurseScan'

    def __init__(self, parent=None):
        QtCore.QAbstractTableModel.__init__(self, parent)
        util.CallbackRegistry.register_save_config(self.save_config)

        # always points to the current root; not None initially so that
        # rowCount() works from the beginning without special casing
        self.root = DirectoryItem(kdecore.KUrl(), parent=None)

        # a mapping kurl.addTrailingslash().url() to DirectoryItems; used from
        # the dirLister slots to access the corresponding item; also, when
        # changing roots, an existing root is searched here first
        self.items = {}

        # a mapping root -> set() of DirectoryItems which have not been feed to
        # the dirLister() yet; populate_next() pop()s from here and feeds to
        # the dirLister; the sets gain items in _dirLister_new_items
        self.pending = {}

        # my precious
        self.dirLister = kio.KDirLister(self)

        # sometimes we need to block until a certain directory has been
        # completed by the dirLister; for that, caller sets block_kurl to the
        # desired directory, and calls exec(); _dirLister_completed calls
        # exit() as appropriate
        self.block_kurl = None
        self.event_loop = QtCore.QEventLoop(self)

        # this will change to a random object() each time the limit pattern
        # changes; if there is no active pattern, it will be None; items store
        # it together with the "visible" flag to know if the flag value is
        # still valid
        self.patternId = None

        # whether we'll recurse into the directory hierarchy by ourselves, or
        # only on demand; has a property below, which LeftSide uses
        config = kdecore.KGlobal.config().group(self.CONFIG_SECTION)
        self._recurse = config.readEntry(
                self.CONFIG_RECURSE_OPTION, QtCore.QVariant(False)).toBool()

        ##

        self.dirLister.setMimeFilter(['inode/directory'] +
                            minirok.Globals.engine.mime_types())

        self.connect(self.dirLister,
                QtCore.SIGNAL('newItems(const KFileItemList &)'),
                self._dirLister_new_items)

        self.connect(self.dirLister,
                QtCore.SIGNAL('completed(const KUrl &)'),
                self._dirLister_completed)

        self.connect(self.dirLister,
                QtCore.SIGNAL('itemsDeleted(const KFileItemList &)'),
                self._dirLister_delete_items)

    def _set_recurse(self, value):
        if self._recurse ^ value:
            self._recurse = bool(value)
            if self._recurse:
                self.populate_next()

    recurse = property(lambda self: self._recurse, _set_recurse)

    ##

    """Model functions."""

    def columnCount(self, parent):
        return 1

    def rowCount(self, parent):
        if parent.column() > 0:
            return 0
        elif not parent.isValid():
            item = self.root
        else:
            item = parent.internalPointer()

        return len(item.children) # TODO: use children[-1].row + 1?

    def index(self, row, column, parent):
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()

        if not parent.isValid():
            item = self.root
        else:
            item = parent.internalPointer()

        try:
            return self.createIndex(row, column, item.children[row])
        except IndexError:
            return QtCore.QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QtCore.QModelIndex()

        item = index.internalPointer()
        parent = item.parent

        if parent is self.root:
            return QtCore.QModelIndex()
        else:
            return self.createIndex(parent.row, 0, parent)

    def data(self, index, role):
        if not index.isValid():
            return QtCore.QVariant()
        else:
            item = index.internalPointer()

        if role == Qt.DisplayRole:
            return QtCore.QVariant(item.name)
        else:
            return QtCore.QVariant()

    def flags(self, index):
        if index.isValid():
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        else:
            return Qt.ItemFlag()

    def hasChildren(self, parent):
        if not parent.isValid():
            item = self.root
        else:
            item = parent.internalPointer()

        if not item.IS_DIR:
            return False
        elif not item.populated:
            return True
        else:
            return bool(item.children)

    def canFetchMore(self, parent):
        if not parent.isValid():
            return False # self.root is always fetched
        else:
            item = parent.internalPointer()
            if not item.IS_DIR:
                return False
            else:
                return not item.populated

    def fetchMore(self, parent):
        if parent.isValid():
            item = parent.internalPointer()
            self.ensure_populated(item)

    def mimeData(self, indexes):
        urls = self.urls_from_indexes(indexes)
        self.mimedata = mimedata = QtCore.QMimeData() # "self" needed
        kurllist = kdecore.KUrl.List(urls)
        kurllist.populateMimeData(mimedata)
        mimedata.setData(DRAG_MIME_TYPE, 'True')
        return self.mimedata

    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction

    ##

    """Generic slots."""

    def slot_change_url(self, url):
        """Changes the root of the model to the specified URL."""
        kurl = kdecore.KUrl(url)
        key = _urlkey(kurl)

        try:
            item = self.items[key]
        except KeyError:
            item = self.items[key] = DirectoryItem(kurl, parent=None)
            self.pending[item] = set([ item ])
        else:
            if item is self.root:
                return

        self.root = item
        self.reset()
        self.emit(QtCore.SIGNAL('scan_in_progress'), True)
        self.populate_next()

    def slot_reload(self):
        """Forces a reload of the current root.

        This is done by dropping the root DirectoryItem from self.items.
        """
        key = _urlkey(self.root.kurl)
        try:
            self.items.pop(key)
        except KeyError:
            pass # :-?

        self.slot_change_url(self.root.kurl)

    def populate_next(self):
        try:
            item = self.pending[self.root].pop()
        except KeyError:
            self.emit(QtCore.SIGNAL('scan_in_progress'), False)
        else:
            self.dirLister.openUrl(item.kurl,
                    kio.KDirLister.OpenUrlFlag(
                        kio.KDirLister.Keep | kio.KDirLister.Reload))

    def save_config(self):
        config = kdecore.KGlobal.config().group(self.CONFIG_SECTION)
        config.writeEntry(self.CONFIG_RECURSE_OPTION,
                            QtCore.QVariant(self._recurse))

    ##

    """Slots for KDirLister."""

    def _dirLister_new_items(self, entries):
        key = _urlkey(entries[0].url(), up=True)
        try:
            parent = self.items[key]
        except KeyError:
            # If a parent can't be found in self.items, it is because it
            # was dropped for being empty. We create its DirectoryItem again,
            # and those of all its missing parents, but we take care not to
            # feed them to the KDirLister (by setting their populated attribute
            # to True), because they are already there.
            kurl = entries[0].url().upUrl()
            missing = []
            while True:
                key = _urlkey(kurl)
                try:
                    parent = self.items[key]
                except KeyError:
                    missing.append(kurl)
                    if kurl == kurl.upUrl():
                        minirok.logger.warn('could not find a parent for %s',
                                            entries[0].url.prettyUrl())
                        return
                    else:
                        kurl = kurl.upUrl()
                else:
                    break
            for kurl in reversed(missing):
                item = DirectoryItem(kurl, parent)
                item.populated = True # This must come before insert_children()
                self.insert_children(parent, [item])
                parent = item

        cls = [ FileItem, DirectoryItem ]
        newitems = [ cls[e.isDir()](e.url(), parent) for e in entries ]
        newitems.sort()
        self.insert_children(parent, newitems)

    def insert_children(self, parent, items):
        """Insert items into parent.children.

        items must be sorted already.
        """
        if parent.root is not self.root:
            index = None
            beginInsertRows = endInsertRows = lambda *x: None
        else:
            endInsertRows = self.endInsertRows
            beginInsertRows = self.beginInsertRows

            if parent is self.root:
                index = QtCore.QModelIndex()
            else:
                index = self.createIndex(parent.row, 0, parent)

        def myinsert(mylist, items):
            """Insert items into mylist so that it remains sorted.

            Both mylist and items must be sorted already. The function will
            call begin/endInsertRows (defined above) as appropriate.
            """
            pos = bisect.bisect_right(mylist, items[0])
            if mylist[pos:]:
                upto = 1
                for item in items[1:]:
                    if item < mylist[pos+1]:
                        upto += 1
                    else:
                        break
            else:
                upto = len(items)

            beginInsertRows(index, pos, pos + upto - 1)
            for i, item in enumerate(items[0:upto]):
                item.row = i + pos
            mylist[pos:0] = items[0:upto]
            endInsertRows()

            if upto < len(items):
                myinsert(mylist, items[upto:])

        myinsert(parent.children, items)

        ##

        diritems = set(x for x in items if x.IS_DIR)
        for diritem in diritems:
            self.items[_urlkey(diritem.kurl)] = diritem

        self.pending[parent.root].update(d for d in diritems if not d.populated)

    def _dirLister_completed(self, kurl):
        key = _urlkey(kurl)

        if (self.block_kurl is not None
                and key == self.block_kurl.url()):
            assert self.event_loop.isRunning()
            self.block_kurl = None
            self.event_loop.exit()

        try:
            item = self.items[key]
        except KeyError:
            pass # Emptied item from delete_items()
        else:
            item.populated = True
            self.pending[item.root].discard(item) # err, don't we pop() above?
            if item.parent and not item.children:
                self.delete_children(item.parent, [ item.row ])

        if self._recurse:
            self.populate_next()

    def _dirLister_delete_items(self, entries):
        key = _urlkey(entries[0].url(), up=True)
        try:
            parent = self.items[key]
        except KeyError:
            minirok.logger.error(
                    'key not found in itemsDeleted: %r', unicode(key))
            return

        have_names = dict((unicode(x.name), i)
                            for i, x in enumerate(parent.children))
        drop_indices = []
        for e in entries:
            name = unicode(e.url().fileName())
            try:
                drop_indices.append(have_names[name])
            except KeyError:
                minirok.logger.warn(
                    'unknown item to delete: %s', e.url().prettyUrl())

        self.delete_children(parent, drop_indices)

    def delete_children(self, parent, indices):
        """Drop those items from parent.children indicated by indices."""
        if parent.root is not self.root:
            index = None
            beginRemoveRows = endRemoveRows = lambda *x: None
        else:
            endRemoveRows = self.endRemoveRows
            beginRemoveRows = self.beginRemoveRows

            if parent is self.root:
                index = QtCore.QModelIndex()
            else:
                index = self.createIndex(parent.row, 0, parent)

        for start, amount in reversed(util.contiguous_chunks(indices)):
            beginRemoveRows(index, start, start + amount - 1)
            for child in parent.children[start:start+amount]:
                if child.IS_DIR:
                    self.items.pop(_urlkey(child.kurl))
            parent.children[start:start+amount] = []
            for item in parent.children[start:]:
                item.row -= amount
            endRemoveRows()

        if parent.parent and not parent.children:
            self.delete_children(parent.parent, [ parent.row ])

    ##

    """Other."""

    def ensure_populated(self, item):
        if item.populated:
            return

        self.block_kurl = item.kurl
        self.dirLister.openUrl(item.kurl, self.dirLister.Keep) # XXX racy?; and Reload here too?
        self.event_loop.exec_(QtCore.QEventLoop.ExcludeUserInputEvents)

    def urls_from_indexes(self, indexes):
        kurls = []
        items = [ x.internalPointer() for x in indexes ]

        def _add_item(item):
            if item.IS_DIR:
                self.ensure_populated(item)
                for child in item.children:
                    _add_item(child)
            elif self.patternId is None or item.visible[0]:
                kurl = item.kurl
                if kurl not in kurls:
                    kurls.append(kurl)

        for item in items:
            _add_item(item)

        return kurls

    ##

    """Filtering, outsourced from the Proxy below."""

    def newPattern(self, pattern):
        if unicode(pattern).strip():
            self.patternId = object()
        else:
            self.patternId = None

    def filterAcceptsIndex(self, index, regexes):
        def update_visibility(item):
            if item.IS_DIR:
                visible = False
                for i in item.children:
                    update_visibility(i)
                    visible |= i.visible[0]
            else:
                for regex in regexes:
                    if not regex.search(item.relpath):
                        visible = False
                        break
                else:
                    visible = True

            item.visible = (visible, self.patternId)

        ##

        if self.patternId is None:
            return True
        else:
            item = index.internalPointer()

            if item.visible[1] is not self.patternId:
                update_visibility(item)

            return item.visible[0]

##

class Proxy(proxy.Model):

    # OK, this is a bit dumb. We don't let this proxy do sorting, and cook it
    # up ourselves in _dirLister_new_items(), so that urls_from_indexes() has
    # access to sorted data. And we can't use the stock filterAcceptsRow()
    # because that hides parents that have visible children (grrr), so we
    # provide an implementation that outsources the matching to the source
    # model...

    @proxy._map
    def hasChildren(self, parent):
        pass

    @proxy._map_many
    def urls_from_indexes(self, indexes):
        pass

    ##

    def setPattern(self, pattern):
        self.sourceModel().newPattern(pattern)
        proxy.Model.setPattern(self, pattern)

    def filterAcceptsRow(self, row, parent):
        source = self.sourceModel()
        return source.filterAcceptsIndex(
                source.index(row, 0, parent), self.regexes)

##

class TreeItem(object):

    __slots__ = [ 'kurl', 'parent', 'name', 'relpath', 'visible', 'row' ]

    def __init__(self, kurl, parent):
        self.row = 0
        self.kurl = kurl
        self.parent = parent
        self.name = kurl.fileName()
        self.visible = (True, None) # bool, patternId

        if parent is None:
            self.relpath = ''
        else:
            self.relpath = unicode(kurl.relativeUrl(parent.root.kurl, kurl))

    def __lt__(self, other):
        """Compare two TreeItems, always sorting directories before files."""
        # Provide __lt__ rather than __cmp__, because __cmp__ would get used
        # by DirectoryItem.children.index(), which we want to avoid.
        if self.IS_DIR ^ other.IS_DIR:
            return self.IS_DIR
        else:
            return self.name.localeAwareCompare(other.name) < 0

class FileItem(TreeItem):

    IS_DIR = False
    children = ()


class DirectoryItem(TreeItem):

    IS_DIR = True

    __slots__ = [ 'children', 'populated', 'root' ]

    def __init__(self, kurl, parent):
        TreeItem.__init__(self, kurl, parent)
        self.children = []
        self.populated = False

        if self.parent is None:
            self.root = self
        else:
            self.root = self.parent.root

        self.kurl.adjustPath(self.kurl.AddTrailingSlash) # for relativeUrl()

##

def _urlkey(kurl, up=False):
    """Consistent creating of keys for Model.items from KUrls."""
    if up:
        kurl = kurl.upUrl()
    kurl.adjustPath(kurl.AddTrailingSlash)
    return kurl.url()
