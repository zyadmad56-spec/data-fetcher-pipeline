from setuptools import setup, find_packages

setup(
    name="data-fetcher-pipeline",
    version="1.0.0",
    description="A professional-grade, architecture-driven retrieval pipeline for fetching raw datasets.",
    packages=["scripts"],
    install_requires=[
        "pandas>=2.0.0",
        "requests>=2.31.0",
        "python-dotenv>=1.0.0",
        "yfinance>=0.2.36",
        "beautifulsoup4>=4.12.0",
        "lxml>=5.1.0",
        "html5lib>=1.1",
        "openml>=0.14.0",
        "openpyxl>=3.0.0",
        "kaggle>=1.5.0",
    ],
    entry_points={
        "console_scripts": [
            "data-fetcher=scripts.fetcher_engine:main",
        ],
    },
)
