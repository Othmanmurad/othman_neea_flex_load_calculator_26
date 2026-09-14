Option Explicit

Private Const SUPPORTED_VERSION As String = "1"

Sub ImportDevice()

    Dim xmlDoc As Object
    Dim ws As Worksheet
    Dim FileName As Variant
    Dim RootName As String
    Dim Version As String
    Dim blockStart As Long

    FileName = Application.GetOpenFilename( _
        "XML Files (*.xml),*.xml", _
        , "Select Device XML")

    If FileName = False Then Exit Sub

    Set xmlDoc = CreateObject("MSXML2.DOMDocument.6.0")
    xmlDoc.async = False
    xmlDoc.validateOnParse = False

    If Not xmlDoc.Load(FileName) Then
        MsgBox "Unable to load XML file.", vbCritical
        Exit Sub
    End If

    Set ws = Worksheets("Lookup")

    '------------------------------------
    'Check XML Version
    '------------------------------------
    Version = GetXMLValue(xmlDoc, "//Version")

    If Version <> SUPPORTED_VERSION Then
        MsgBox "Unsupported XML version." & vbCrLf & _
               "Expected: " & SUPPORTED_VERSION & vbCrLf & _
               "Found: " & Version, vbCritical
        Exit Sub
    End If

    '------------------------------------
    'Determine Device Type -> Lookup block
    '------------------------------------
    RootName = UCase(xmlDoc.DocumentElement.nodeName)
    blockStart = GetBlockStart(RootName)

    If blockStart = 0 Then
        MsgBox "Unsupported device type: " & RootName & vbCrLf & _
               "(Only HPWH and ERWH are configured so far.)", vbCritical
        Exit Sub
    End If

    Select Case RootName
        Case "HPWH":  ImportHPWH xmlDoc, ws, blockStart
        Case "ERWH":  ImportERWH xmlDoc, ws, blockStart
    End Select

    'Import common connectivity fields (Order 12-20, offsets 11-19)
    ImportConnectivity xmlDoc, ws, blockStart

    'Write device name to Calculator!I7
    Worksheets("Calculator").Range("I7").Value = GetXMLValue(xmlDoc, "//DeviceName")

    'Reflect the newly imported type as the active FLType
    Dim flType As String
    flType = ws.Cells(blockStart, 3).Value
    Worksheets("Calculator").Range("M6").Value = flType

    MsgBox "Device imported successfully: " & GetXMLValue(xmlDoc, "//DeviceName"), vbInformation

End Sub


'------------------------------------
' Maps XML root element -> Lookup block start row
'------------------------------------
Private Function GetBlockStart(RootName As String) As Long
    Select Case RootName
        Case "HPWH":    GetBlockStart = 4
        Case "ERWH":    GetBlockStart = 25
        Case "HVAC":    GetBlockStart = 46
        Case "EV":      GetBlockStart = 67
        Case "DRYER":   GetBlockStart = 88
        Case "BATTERY": GetBlockStart = 109
        Case "GENERIC": GetBlockStart = 130
        Case Else:      GetBlockStart = 0
    End Select
End Function


'------------------------------------
' Writes one value into Lookup column G (Value) at blockStart + offset
' offset is 0-based: offset 0 = Order 1 (first row of the block)
'------------------------------------
Private Sub WriteLookup(ws As Worksheet, blockStart As Long, offset As Long, val As String)
    ws.Cells(blockStart + offset, 7).Value = val
End Sub


Private Sub ImportHPWH(xmlDoc As Object, ws As Worksheet, blockStart As Long)

    WriteLookup ws, blockStart, 0, GetXMLValue(xmlDoc, "//CompPwr")             ' Order 1  CompPwr
    WriteLookup ws, blockStart, 1, GetXMLValue(xmlDoc, "//ResistPwr")           ' Order 2  ResistPwr
    WriteLookup ws, blockStart, 2, GetXMLValue(xmlDoc, "//RUCap")               ' Order 3  RUCap
    WriteLookup ws, blockStart, 3, GetXMLValue(xmlDoc, "//RDCap")               ' Order 4  RDCap
    WriteLookup ws, blockStart, 4, GetXMLValue(xmlDoc, "//TankVol")             ' Order 5  TankVol
    WriteLookup ws, blockStart, 5, GetXMLValue(xmlDoc, "//MaxWaterTemp")        ' Order 6  MaxWaterTemp
    WriteLookup ws, blockStart, 6, GetXMLValue(xmlDoc, "//MinWaterTemp")        ' Order 7  MinWaterTemp
    WriteLookup ws, blockStart, 7, GetXMLValue(xmlDoc, "//COPUEF")              ' Order 8  COP/UEF
    WriteLookup ws, blockStart, 8, GetXMLValue(xmlDoc, "//HeatCap")             ' Order 9  HeatCap
    WriteLookup ws, blockStart, 9, GetXMLValue(xmlDoc, "//ResponseTime")        ' Order 10 RespTime
    WriteLookup ws, blockStart, 10, GetXMLValue(xmlDoc, "//CntrlInt")           ' Order 11 CtrlInt

End Sub


Private Sub ImportERWH(xmlDoc As Object, ws As Worksheet, blockStart As Long)

    WriteLookup ws, blockStart, 0, "0"                                         ' Order 1  CompPwr (no compressor)
    WriteLookup ws, blockStart, 1, GetXMLValue(xmlDoc, "//ResistPwr")           ' Order 2  ResistPwr
    WriteLookup ws, blockStart, 2, GetXMLValue(xmlDoc, "//RUCap")               ' Order 3  RUCap
    WriteLookup ws, blockStart, 3, GetXMLValue(xmlDoc, "//RDCap")               ' Order 4  RDCap
    WriteLookup ws, blockStart, 4, GetXMLValue(xmlDoc, "//TankVol")             ' Order 5  TankVol
    WriteLookup ws, blockStart, 5, GetXMLValue(xmlDoc, "//MaxWaterTemp")        ' Order 6  MaxWaterTemp
    WriteLookup ws, blockStart, 6, GetXMLValue(xmlDoc, "//MinWaterTemp")        ' Order 7  MinWaterTemp
    WriteLookup ws, blockStart, 7, GetXMLValue(xmlDoc, "//COPUEF")              ' Order 8  COP/UEF
    WriteLookup ws, blockStart, 8, GetXMLValue(xmlDoc, "//HeatCap")             ' Order 9  HeatCap
    WriteLookup ws, blockStart, 9, GetXMLValue(xmlDoc, "//ResponseTime")        ' Order 10 RespTime
    WriteLookup ws, blockStart, 10, GetXMLValue(xmlDoc, "//CntrlInt")           ' Order 11 CtrlInt

End Sub


'------------------------------------
' Shared Control/Interop fields - Order 12-20 (offsets 11-19)
' DataCnfrmnt / ShiftCnfrmnt stay numeric - no Yes/No conversion.
'------------------------------------
Private Sub ImportConnectivity(xmlDoc As Object, ws As Worksheet, blockStart As Long)

    WriteLookup ws, blockStart, 11, GetXMLValue(xmlDoc, "//Schedule")           ' Order 12 CanSchedule
    WriteLookup ws, blockStart, 12, GetXMLValue(xmlDoc, "//App")                ' Order 13 AppExist
    WriteLookup ws, blockStart, 13, GetXMLValue(xmlDoc, "//Network")            ' Order 14 NetworkConnect
    WriteLookup ws, blockStart, 14, GetXMLValue(xmlDoc, "//Dynamic")            ' Order 15 ConnectDynamicData
    WriteLookup ws, blockStart, 15, GetXMLValue(xmlDoc, "//Standard")           ' Order 16 OpenStandard
    WriteLookup ws, blockStart, 16, GetXMLValue(xmlDoc, "//UCM")                ' Order 17 UCD/DCMReady
    WriteLookup ws, blockStart, 17, GetXMLValue(xmlDoc, "//Compliant")          ' Order 18 Compliant
    WriteLookup ws, blockStart, 18, GetXMLValue(xmlDoc, "//DataCnfrmnt")        ' Order 19 DataConformant
    WriteLookup ws, blockStart, 19, GetXMLValue(xmlDoc, "//ShiftCnfrmnt")       ' Order 20 ResponseConformant

End Sub


Private Function GetXMLValue(xmlDoc As Object, XPath As String) As String
    Dim node As Object
    Set node = xmlDoc.SelectSingleNode(XPath)
    If node Is Nothing Then
        GetXMLValue = ""
    Else
        GetXMLValue = node.Text
    End If
End Function

