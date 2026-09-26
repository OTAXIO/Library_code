"""Import buttons transcribed from the user's screenshot; not database coverage claims."""
CHANNELS = {
    '数据导入': ['数据导入'],
    'WOS': ['WOS数据导入(Excel)', 'WOS数据导入(Txt)'],
    'CSCD': ['CSCD数据导入(Txt)'],
    'CSSCI': ['CSSCI数据导入(Txt)'],
    'CNKI': ['CNKI数据导入(Excel/Txt)'],
    '万方': ['万方数据导入'],
    'EI': ['EI数据导入(Csv/Excel)'],
    'VIP': ['VIP数据导入(Excel)'],
    'SPOP': ['SPOP数据导入(Txt/Excel)'],
}


def route_fields(result):
    """Preserve a model recommendation without manufacturing an executable import."""
    if result is None:
        return None
    channel = result['import_channel']
    return {'recommended_channel':channel,
            'available_buttons':CHANNELS[channel] if channel else [],
            'selected_button':None,
            'status':'候选渠道，待核实收录及导出文件' if channel else '待判定',
            'reason':result['channel_reason']}
