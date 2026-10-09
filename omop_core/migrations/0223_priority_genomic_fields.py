from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0222_genomics_text_values')]
    operations = [
        migrations.AddField(model_name='patientrecord', name=name,
            field=models.JSONField(blank=True, default=list))
        for name in ["genomics_brca1","genomics_brca2","genomics_pik3ca","genomics_tp53","genomics_esr1","genomics_palb1","genomics_kras","genomics_nras","genomics_braf","genomics_myc","genomics_fam46c","genomics_dis3","genomics_xbp1","genomics_bcl2","genomics_ezh2","genomics_kmt2d","genomics_crebbp","genomics_bcl6","genomics_notch1","genomics_notch2","genomics_sf3b1","genomics_atm","genomics_nsd2","genomics_cdkn2a","genomics_smarca4","genomics_ccnd1","genomics_del17p","genomics_t414","genomics_t1114","genomics_t1416","genomics_gain1q","genomics_hyperdiploidy","genomics_chromothripsis","genomics_igh","genomics_bcl2_amplification","genomics_complex_karyotype","genomics_complex_karyotype_excl_t1114","genomics_del11q","genomics_del13q","genomics_trisomy12","genomics_atm_atr","genomics_notch1_notch2"]
    ] + [migrations.AlterField(
        model_name='fieldconceptmapping', name='value_kind',
        field=models.CharField(blank=True, default='', max_length=10,
            help_text='Which value column the fact is written to.',
            choices=[('number', 'Number'), ('string', 'String'), ('date', 'Date'),
                     ('boolean', 'Boolean'), ('json', 'Structured findings')]),
    )]
