import logging
from datetime import date
from HTMLParser import HTMLParser
from time import mktime, strptime
from urlparse import urlparse

from lxml.etree import fromstring as etree_fromstring
from lxml.html import fragment_fromstring, fromstring, tostring
from lxml.html.clean import clean_html

import transaction
from DateTime import DateTime
from plone.api.portal import get_tool
from plone.app.textfield.value import RichTextValue
from plone.dexterity.utils import createContentInContainer as create
from plone.namedfile.file import NamedBlobFile
from Products.Five.browser import BrowserView

logger = logging.getLogger('eionet.theme.importer')

DEBUG = True


def read_data(obj_file):
    ranges = []
    data = obj_file.data

    if isinstance(data, str):
        ranges.append(data)
    else:
        while data is not None:
            ranges.append(data.data)
            data = data.next

    return ''.join(ranges)


class DummyDict:
    """ A "request" substitute that renders all DTML vars as empty strings
    """

    def __getitem__(self, key):
        return ''


def as_plain_text(value):
    value = HTMLParser().unescape(value)

    if isinstance(value, str):
        value = value.decode('utf-8')

    value = u"<div>%s</div>" % value
    el = fragment_fromstring(value)
    texts = el.xpath('text()')

    return u' '.join(texts)


def as_richtext(value):
    if value:
        value = clean_html(value)

    return RichTextValue(value, 'text/html', 'text/html')


def noop(value):
    return value


def as_date(value):
    return DateTime(value).asdatetime()


class EionetContentImporter(BrowserView):
    """ A helper class to import old content exported as XML
    """

    def __call__(self):

        if self.request.method == 'GET':
            return self.index()

        path = self.request.form.get('sourcepath')
        portal_type = self.request.form.get('portal_type')

        obj = self.context.restrictedTraverse(path)

        text = read_data(obj)
        text = text.replace('\f', '')        # line feed, weird
        text = text.decode('utf-8')

        tree = etree_fromstring(text)
        importer = getattr(self, 'import_' + portal_type)

        count = importer(self.context, portal_type, tree)

        return "Imported %s objects" % count

    def import_etc_report(self, context, portal_type, tree):
        _map = {
            'teaser': ('abstract', as_richtext),
            'releasedate': ('publication_date', as_date),
            'title': ('title', as_plain_text),
        }
        count = 0

        for node in tree.xpath('object'):
            url = node.get('url')
            id = url.rsplit('/', 1)[-1]
            props = {}

            for ep in node.findall('prop'):
                pname = ep.get('name')

                if pname in _map:
                    pname, convert = _map[pname]
                else:
                    convert = noop

                text = ep.text or ''
                props[pname] = convert(text.strip())

            try:
                obj = create(context, portal_type, id=id, **props)
            except ValueError:      # this is due to id error
                obj = create(context, portal_type, **props)
                logger.warning("Changed id for object: %s", id)

            logger.info("Imported %s", obj.absolute_url())
            count += 1

        return count


class EionetStructureImporter(BrowserView):
    """ A helper class to import old Zope content and recreate as Plone content
    """

    def __call__(self):
        if self.request.method == 'GET':
            return self.index()

        path = self.request.form.get('sourcepath')
        source = self.context.restrictedTraverse(path)
        self._source = source

        dest = self.import_location(source, self.context)
        logger.info("Finished import")

        return "Finished import: %s" % dest.absolute_url()

    def import_location(self, source, destination):
        for obj in source.objectValues():
            metatype = obj.meta_type.replace(' ', '')
            handler = getattr(self, 'import_' + metatype, None)

            if handler is None:
                logger.warning("Not importing: %s (%s)",
                               (obj.getId(), metatype))
                # raise ValueError('Not supported: %s (%s)' % (obj.getId(),
                #                                              metatype))

                continue
            handler(obj, destination)

        if not DEBUG:
            transaction.commit()

        return destination

    def import_File(self, obj, destination):
        if DEBUG:
            return

        data = read_data(obj)

        ctype = obj.content_type
        tool = get_tool('mimetypes_registry')

        if ctype and (not tool.lookup(ctype)):
            ctype = 'application/pdf'      # should be a safe fallback
        oid = obj.getId().decode('utf-8')

        fobj = NamedBlobFile(data=data, contentType=ctype, filename=oid)

        props = {
            'file': fobj,
            'title': as_plain_text(obj.title),
            'id': oid,
        }

        obj = create(destination, 'File', **props)
        logger.info("Created file: %s", obj.absolute_url())

        transaction.commit()

        return obj

    def import_Image(self, obj, destination):
        return self.import_File(obj, destination)

    def import_Folder(self, obj, destination):
        folder = create(destination, 'Folder', id=obj.getId(),
                        title=as_plain_text(obj.title))
        logger.info("Created folder: %s", obj.absolute_url())

        return self.import_location(obj, folder)

    def import_DTMLDocument(self, obj, destination):
        text = obj(REQUEST=DummyDict())

        title = obj.title

        if isinstance(title, str):
            title = title.decode('utf-8')

        title = title.strip()
        title = as_plain_text(title)

        page = self._create_page(destination, obj.getId(), title, text)

        if page is not None:
            logger.info("Created page: %s", page.absolute_url())

        return page

    def import_DTMLMethod(self, obj, destination):
        return self.import_DTMLDocument(obj, destination)

    def _parse_page(self, title, html):
        if not html:
            return title, html

        html = html.decode('utf-8')
        html = clean_html(html)
        tree = fromstring(html)
        h1s = tree.xpath('h1')

        if h1s:
            h1 = h1s[0]
            h1.drop_tree()
            new_title = h1.text_content().strip()
            new_title = new_title.replace(u'\n', u' - ')
            logger.info("Replacing title: %s -:- %s", title, new_title)
            title = new_title

        html = tostring(tree)

        return title, html

    def _create_page(self, destination, id, title, html):
        title, html = self._parse_page(title, html)

        rt = RichTextValue(html, 'text/html', 'text/html')

        return create(destination, 'Document', id=id, title=title, text=rt)

    def import_SiteErrorLog(self, obj, destination):
        return destination


class EionetDTMLReportImporter(EionetStructureImporter):
    """ A variant of EionetStructureImporter that parses DTMLDocuments
    """

    def import_DTMLDocument(self, obj, destination):
        text = obj(REQUEST=DummyDict())

        title = obj.title

        if isinstance(title, str):
            title = title.decode('utf-8')

        title = title.strip()
        title = as_plain_text(title)

        page = self._create_report(destination, obj.getId(), title, text)

        if page is not None:
            logger.info("Created page: %s", page.absolute_url())

        return page

    def _find_original_file(self, link):
        """ Returns a reference to the original file
        """
        path = urlparse(link).path
        bits = filter(None, path.split('/'))[::-1]
        acc = []
        context_ids = self._source.objectIds()
        context = self._source

        for bit in bits:
            acc.append(bit)

            if bit in context_ids:
                for a in acc[::-1]:
                    context = context.restrictedTraverse(a)

        return context.aq_inner

    def _create_report(self, destination, id, title, html):
        # has 2 files:
        # https://bd.eionet.europa.eu/Reports/ETCBDTechnicalWorkingpapers/Factsheets_Mediterranean_marine_hab_spec
        title, html = self._parse_page(title, html)

        e = fromstring(html)
        fp = e.xpath('//p[contains(text(), "See the")]')

        if fp:      # treat the linked report file(s)
            p = fp[0]
            p.drop_tree()
            file_links = p.xpath('a/@href')

        bs = e.xpath('//b[contains(text(), "Abstract")]')

        if bs:
            bs[0].drop_tree()

        publication_date = None
        brs = e.xpath('//b[contains(text(), "Released in")]')

        if brs:
            br = brs[0]
            human_date = br.tail.strip()
            br.tail = ''
            br.drop_tree()

            try:
                tstruct = strptime(human_date, '%B %Y')
            except ValueError:
                logger.warning("Could not parse date: %s", human_date)
            else:
                d = date.fromtimestamp(mktime(tstruct))
                publication_date = d    # DateTime(d.year, d.month, d.day)

        html = tostring(e, pretty_print=True)
        rt = RichTextValue(html, 'text/html', 'text/html')

        report = create(destination, 'etc_report',
                        id=id, title=title, abstract=rt,
                        publication_date=publication_date)

        logger.info("Created report: %s", report)

        for link in file_links:
            fobj = self._find_original_file(link)
            # logger.info("Got file: %s for link %s", fobj, link)
            self.import_File(fobj, report)

        return report
