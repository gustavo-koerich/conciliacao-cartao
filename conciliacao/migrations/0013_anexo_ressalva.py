from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conciliacao', '0012_controle_de_compras'),
    ]

    operations = [
        migrations.AddField(
            model_name='anexo',
            name='ressalva',
            field=models.CharField(blank=True, help_text='Casamento pouco confiável (só pelo valor): o lançamento fica conciliado com ressalva.', max_length=200),
        ),
    ]
