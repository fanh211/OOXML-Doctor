"""构造一个自包含的最小 PPTX，用于单元测试。

**刻意不使用** Unit1 那套学科网课件做测试夹具 —— 那是第三方版权内容，
不应进入发行物。单元测试只用这里合成的夹具；真实样本仅作本地校准。
"""
from __future__ import annotations

import os
import zipfile

CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Default Extension="JPG" ContentType="image/.jpg"/>
<Default Extension="png" ContentType="image/png"/>
<Default Extension="mp3" ContentType="audio/mp3"/>
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
<Override PartName="/ppt/tags/tag1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tags+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

PRESENTATION = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>
"""

PRESENTATION_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""

# 注入的缺陷：
#   D1  <p:nvPr> 内 extLst 排在 custDataLst 之前              -> M1.20（会触发 Office 修复）
#   D2  <a:hlinkClick r:id=""/>                               -> P4-ASC 悬空引用
#   D3  同一 spid 上两个媒体节点 video + audio                -> P4-ASC 媒体重复
#   D4  rId3 audio Target="NULL" TargetMode="External"        -> APP-REL-PLACEHOLDER
SLIDE1 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:pic><p:nvPicPr><p:cNvPr id="2" name="speaker"><a:hlinkClick r:id="" action="ppaction://media"/></p:cNvPr><p:cNvPicPr/><p:nvPr><a:audioFile r:link="rId3"/><p:extLst><p:ext uri="{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}"><p14:media xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" r:embed="rId4"/></p:ext></p:extLst><p:custDataLst><p:tags r:id="rId5"/></p:custDataLst></p:nvPr></p:nvPicPr><p:blipFill><a:blip r:embed="rId6"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr/></p:pic></p:spTree></p:cSld><p:timing><p:tnLst><p:par><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst><p:video><p:cMediaNode vol="80000"><p:cTn id="10"/><p:tgtEl><p:spTgt spid="2"/></p:tgtEl></p:cMediaNode></p:video><p:audio><p:cMediaNode vol="80000"><p:cTn id="11"/><p:tgtEl><p:spTgt spid="2"/></p:tgtEl></p:cMediaNode></p:audio></p:childTnLst></p:cTn></p:par></p:tnLst></p:timing></p:sld>
"""

SLIDE1_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio" Target="NULL" TargetMode="External"/>
<Relationship Id="rId4" Type="http://schemas.microsoft.com/office/2007/relationships/media" Target="../media/media1.mp3"/>
<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tags" Target="../tags/tag1.xml"/>
<Relationship Id="rId6" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>
</Relationships>
"""

TAG1 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:tagLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:tag name="KSO_WM_UNIT_ID" val="_1**"/><p:tag name="AS_UNIQUEID" val="7"/></p:tagLst>
"""

APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>WPS 演示</Application><Slides>3</Slides><Notes>9</Notes><MMClips>1</MMClips><AppVersion>14.0000</AppVersion></Properties>
"""

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def build(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("ppt/presentation.xml", PRESENTATION)
        z.writestr("ppt/_rels/presentation.xml.rels", PRESENTATION_RELS)
        z.writestr("ppt/slides/slide1.xml", SLIDE1)
        z.writestr("ppt/slides/_rels/slide1.xml.rels", SLIDE1_RELS)
        z.writestr("ppt/tags/tag1.xml", TAG1)
        z.writestr("docProps/app.xml", APP)
        z.writestr("ppt/media/media1.mp3", MP3)
        z.writestr("ppt/media/image1.png", PNG)
    return path


BODY_STYLE = (
    '<p:txStyles><p:titleStyle/><p:bodyStyle><a:lvl1pPr><a:buSzPct val="80000"/></a:lvl1pPr>'
    '</p:bodyStyle><p:otherStyle/></p:txStyles>'
)


def build_pattern_violation(path: str) -> str:
    """干净包 + 一处**取值性**违规：bullet 尺寸必须落在 25%–400%，这里写 80000。

    真实样本校准依据：PowerPoint 官方修复版仍然保留同样的值，
    说明取值类违规不会触发"需要修复"，必须报 warning 而不是 error。
    """
    build_clean(path)
    import zipfile as _z
    master = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
              '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
              '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>'
              '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
              'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
              'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
              + BODY_STYLE.replace('<p:titleStyle/><p:bodyStyle>', '<p:titleStyle/><p:bodyStyle>')
              + '</p:sldMaster>')
    with _z.ZipFile(path, 'a') as z:
        z.writestr('ppt/slideMasters/slideMaster1.xml', master)
    return path


def build_clean(path: str) -> str:
    """同一结构，但缺陷全部修好 —— 用来验证“零假阳性”。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ct = (CT.replace('ContentType="image/.jpg"', 'ContentType="image/jpeg"')
            .replace('ContentType="audio/mp3"', 'ContentType="audio/mpeg"'))
    slide = (SLIDE1
             .replace('<a:hlinkClick r:id="" action="ppaction://media"/>',
                      '<a:hlinkClick r:id="rId7" action="ppaction://media"/>')
             .replace('<p:extLst><p:ext uri="{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}">'
                      '<p14:media xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" '
                      'r:embed="rId4"/></p:ext></p:extLst><p:custDataLst><p:tags r:id="rId5"/>'
                      '</p:custDataLst>',
                      '<p:custDataLst><p:tags r:id="rId5"/></p:custDataLst>'
                      '<p:extLst><p:ext uri="{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}">'
                      '<p14:media xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" '
                      'r:embed="rId4"/></p:ext></p:extLst>')
             .replace('<p:video><p:cMediaNode vol="80000"><p:cTn id="10"/><p:tgtEl>'
                      '<p:spTgt spid="2"/></p:tgtEl></p:cMediaNode></p:video>', '')
             .replace('<a:audioFile r:link="rId3"/>', '<a:audioFile r:link="rId8"/>'))
    rels = SLIDE1_RELS.replace(
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio" Target="NULL" TargetMode="External"/>',
        '<Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.org/" TargetMode="External"/>'
        '<Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio" Target="../media/media1.mp3"/>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("ppt/presentation.xml", PRESENTATION)
        z.writestr("ppt/_rels/presentation.xml.rels", PRESENTATION_RELS)
        z.writestr("ppt/slides/slide1.xml", slide)
        z.writestr("ppt/slides/_rels/slide1.xml.rels", rels)
        z.writestr("ppt/tags/tag1.xml", TAG1)
        z.writestr("docProps/app.xml", APP)
        z.writestr("ppt/media/media1.mp3", MP3)
        z.writestr("ppt/media/image1.png", PNG)
    return path
