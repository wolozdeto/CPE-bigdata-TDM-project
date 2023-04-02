import os
import io
import spacy
import folium
import datetime
import squarify
import webcolors
import collections
import numpy as np
import pandas as pd
from tqdm import tqdm
import mysql.connector
from dotenv import load_dotenv
from collections import Counter
import matplotlib.pyplot as plt
from geopy.geocoders import Nominatim
from scipy.spatial.distance import pdist
from flask import Flask, Response, request
from scipy.cluster.hierarchy import dendrogram, linkage

load_dotenv()

app = Flask(__name__)

# Set the base folder path for the project
output_path = "../output"
# Path to save metadata
metadata_path = './metadata'

# Set SQL variables
sql_host = os.getenv("SQL_HOST")
sql_user = os.getenv("SQL_USER")
sql_password = os.getenv("SQL_PASSWORD")
sql_database = os.getenv("SQL_DATABASE")


def get_metadata_from_mariadb_db(db_name='bigdata', user='root', password='', host='localhost', port='3306'):
    """
    Get the metadata from the MariaDB database

    :param db_name: The name of the database
    :param user: The username to connect to the database
    :param password: The password to connect to the database
    :param host: The hostname or IP address of the database server
    :param port: The port number to connect to the database server
    :return: A dictionary with the metadata
    """
    print("Connecting to database...")

    # Open a connection to the database
    conn = mysql.connector.connect(
        user=user,
        password=password,
        host=host,
        port=port,
        database=db_name
    )
    # Create a cursor
    c = conn.cursor()

    print("Retrieving metadata from database...")

    # Retrieve the metadata
    c.execute("""
        SELECT filename, GROUP_CONCAT(CONCAT(mkey, '\t', mvalue) SEPARATOR '\n') AS metadata
        FROM metadata
        GROUP BY filename;
    """)
    metadata = c.fetchall()

    # Close the connection
    conn.close()

    # Convert the metadata to a dictionary
    result = {}
    for image in tqdm(metadata, desc="Get metadata from database"):
        try:
            result[image[0]] = {}
            props = image[1].split('\n')
            for prop in props:
                if prop:
                    k, value = prop.split('\t')
                    result[image[0]][k] = value
        except Exception as e:
            print(e, image)

    return result


def dms_to_decimal(degrees, minutes, seconds):
    """
    Convert DMS (degrees, minutes, seconds) coordinates to DD (decimal degrees)
    :param degrees: degrees
    :param minutes: minutes
    :param seconds:  seconds
    :return: decimal coordinates
    """
    decimal_degrees = abs(degrees) + (minutes / 60) + (seconds / 3600)

    if degrees < 0:
        decimal_degrees = -decimal_degrees

    return decimal_degrees


def clean_gps_infos(metadata_to_cln):
    """
    Clean the GPS infos

    :param metadata_to_cln: The metadata to clean
    :return: A dictionary with the cleaned metadata
    """

    cpt_valid, cpt_invalid, cpt_converted = 0, 0, 0
    for file in tqdm(metadata_to_cln, desc="Clean GPS values"):
        file_meta = metadata_to_cln[file]

        if 'Latitude' in file_meta:
            has_dms_values = file_meta['LatitudeDegrees'] != '0.000000' or file_meta['LongitudeDegrees'] != '0.000000'
            has_decimal_values = file_meta['Latitude'] != '0.000000' or file_meta['Longitude'] != '0.000000'

            if has_dms_values or has_decimal_values:
                should_convert = '.' not in file_meta['Latitude'] and has_dms_values

                if should_convert:
                    # calculate the decimal coordinates from the degrees coordinates
                    latitude = dms_to_decimal(
                        float(file_meta['LatitudeDegrees']),
                        float(file_meta['LatitudeMinutes']),
                        float(file_meta['LatitudeSeconds']))

                    longitude = dms_to_decimal(
                        float(file_meta['LongitudeDegrees']),
                        float(file_meta['LongitudeMinutes']),
                        float(file_meta['LongitudeSeconds']))

                    cpt_converted += 1
                else:
                    # convert the coordinates to float
                    latitude = float(file_meta['Latitude'])
                    longitude = float(file_meta['Longitude'])

                # update the metadata with the calculated latitude and longitude
                metadata_to_cln[file]['Latitude'] = latitude
                metadata_to_cln[file]['Longitude'] = longitude
                cpt_valid += 1

            else:
                metadata_to_cln[file]['Latitude'] = None
                metadata_to_cln[file]['Longitude'] = None
                metadata_to_cln[file]['Altitude'] = None
                cpt_invalid += 1

    print("GPS values : \n",
          "Valid : ", cpt_valid,
          "\nInvalid : ", cpt_invalid,
          "\nConverted : ", cpt_converted,
          )

    return metadata_to_cln


def clean_metadata(metadata_to_clean):
    """
    Clean the metadata
    Remove special characters from the 'Make' property values
    Remove the 'T' and '-' characters from the 'DateTime' property values

    :param metadata_to_clean: The metadata to clean
    :return: A dictionary with the cleaned metadata
    """
    cln_meta = metadata_to_clean.copy()

    # Clean 'Make' property values
    try:
        for file in tqdm(cln_meta, desc="Clean 'Make' property values"):
            if 'Make' in cln_meta[file]:
                cln_meta[file]['Make'] = ''.join(filter(str.isalpha, cln_meta[file]['Make'])).replace('CORPORATION',
                                                                                                      '').replace(
                    'CORP', '').replace('COMPANY', '').replace('LTD', '').replace('IMAGING', '')
    except Exception as e:
        print(e)

    # Clean 'DateTime' property values
    cpt, cpt_error = 0, 0
    date_error = []
    try:

        for file in tqdm(cln_meta, desc="Clean 'DateTime' property values"):
            if 'DateTimeOriginal' in cln_meta[file]:
                date = cln_meta[file]['DateTimeOriginal']
                try:
                    if date is not None:
                        tmp = date.replace('T', ' ').replace('-', ':').split('+')[0]
                        cln_meta[file]['DateTimeOriginal'] = datetime.datetime.strptime(tmp[:19], '%Y:%m:%d %H:%M:%S')
                        # if the year is after actual year, we assume that the date is wrong
                        if cln_meta[file]['DateTimeOriginal'].year > datetime.datetime.now().year:
                            date_error.append(cln_meta[file]['DateTimeOriginal'])
                            cln_meta[file]['DateTimeOriginal'] = None
                            cpt_error += 1
                        else:
                            cpt += 1
                except ValueError:
                    date_error.append(date)
                    cln_meta[file]['DateTimeOriginal'] = None
                    cpt_error += 1
    except Exception as e:
        print(e)

    print(f"Metadata cleaned ! {cpt}/{len(cln_meta)} dates OK, {cpt_error} dates KO")
    print(f"Dates KO : {date_error}")

    # Clean 'tags' property values
    for file in tqdm(cln_meta, desc="Clean 'tags' property values"):
        if 'tags' in cln_meta[file]:
            val = None
            if cln_meta[file]['tags'] is not None:
                val = eval(cln_meta[file]['tags'])
            cln_meta[file]['tags'] = val

    # Clean the GPS infos
    cln_meta = clean_gps_infos(cln_meta)

    return cln_meta


@app.route('/metadata', methods=['GET'])
def get_metadata():
    """
    Get the metadata from the database
    :return: A JSON object with the metadata
    """
    # Check if the metadata file already exists
    if os.path.isfile('mon_dataframe.csv'):
        # If the file exists, read it
        return pd.read_csv('mon_dataframe.csv')
    else:
        # Get the metadata from the database
        brut_metadata = get_metadata_from_mariadb_db(sql_database, sql_user, sql_password, sql_host)
        # Clean the metadata
        cln_metadata = clean_metadata(brut_metadata)
        # Convert the metadata to a DataFrame
        df_metadata = pd.DataFrame.from_dict(cln_metadata).transpose()
        # Fill the 'Make' property NaN values with 'Undefined'
        df_metadata['Make'].fillna('Undefined', inplace=True)

        df_metadata.to_csv(metadata_path + '/metadata.csv')

        # If the function is called into the code, return the DataFrame
        if request.method is None:
            return df_metadata
        else :
            # If the function is called from the API, return the DataFrame as a JSON object
            return df_metadata.to_json(orient='index')


def display_bar(title, x_label, y_label, x_values, y_values, colors=None, rotation=90):
    """
    Display a bar chart

    :param title: The title of the chart
    :param x_label: The x-axis label
    :param y_label: The y-axis label
    :param x_values: The values of the x-axis
    :param y_values: The values of the y-axis
    :param colors: The colors of the bars
    :param rotation: The rotation of the x-axis labels
    """

    fig, ax = plt.subplots()
    ax.bar(x_values, y_values, color=colors)
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_xticklabels(x_values, rotation=rotation)

    # Save the plot to a buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)

    # Return the buffer contents as a response
    return Response(buffer.getvalue(), mimetype='image/png')


def display_pie(title, values, labels, colors=None, autopct="%1.1f%%", legend_title=None, legend_loc=None,
                legend_margin=None):
    """
    Display a pie chart

    :param title: The title of the chart
    :param values: The values of the chart
    :param labels: The labels of the chart
    :param colors: The colors of the chart
    :param autopct: The percentage format
    :param legend_title: The title of the legend,
    :param legend_loc: The location of the legend
    :param legend_margin: The margin of the legend
    """
    fig, ax = plt.subplots()
    ax.pie(values, labels=labels, autopct=autopct, colors=colors)
    if legend_title is not None or legend_loc is not None or legend_margin is not None:
        ax.legend(title=legend_title, loc=legend_loc, bbox_to_anchor=legend_margin)
    ax.set_title(title)

    # Save the plot to a buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)

    # Return the buffer contents as a response
    return Response(buffer.getvalue(), mimetype='image/png')


def display_curve(title, x_label, y_label, x_values, y_values, rotation=90):
    """
    Display a curve

    :param title: The title of the curve
    :param x_label: The label of the x_axis
    :param y_label: The label of the y_axis
    :param x_values: The values of the x_axis
    :param y_values: The values of the y_axis
    :param rotation: The rotation of the x_axis labels
    """

    fig, ax = plt.subplots()
    ax.plot(x_values, y_values)
    ax.set_xticklabels(x_values, rotation=rotation)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)

    # Save the plot to a buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)

    # Return the buffer contents as a response
    return Response(buffer.getvalue(), mimetype='image/png')


def display_histogram(title, x_label, y_label, x_values, bins=10, rotation=90):
    """
    Display a histogram

    :param title: The title of the histogram
    :param x_label: The label of the x_axis
    :param y_label: The label of the y_axis
    :param x_values: The values of the x_axis
    :param bins: The number of bins
    :param rotation: The rotation of the x_axis labels
    """

    fig, ax = plt.subplots()
    ax.hist(x_values, bins=bins)
    ax.set_xticklabels(x_values, rotation=rotation)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)

    # Save the plot to a buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)

    # Return the buffer contents as a response
    return Response(buffer.getvalue(), mimetype='image/png')


def display_tree_map(title, sizes, labels, colors, alpha=0.6):
    """
    Display a tree map

    :param title: The title of the tree map
    :param sizes: The sizes of the tree map
    :param labels: The labels of the tree map
    :param colors: The colors of the tree map
    :param alpha: The alpha of the tree map
    """
    fig, ax = plt.subplots()
    squarify.plot(sizes=sizes, label=labels, color=colors, alpha=alpha, ax=ax)
    ax.set_title(title)

    # Save the plot to a buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)

    # Return the buffer contents as a response
    return Response(buffer.getvalue(), mimetype='image/png')


@app.route('/graph/size', methods=['POST'])
def graph_images_size(df_meta, nb_intervals=7, graph_type='all'):
    return graph_images_size_dynamic(df_meta, nb_intervals, graph_type)


@app.route('/graph/size/static', methods=['POST'])
def graph_images_size_static(interval_size=200, nb_intervals=4):
    """
    Graph the number of images per size category
    The interval size is 200 by default

    :param interval_size: The size of the intervals
    :param nb_intervals: The number of intervals
    """

    df_meta = get_metadata()

    # Calculate the minimum size of each image and store it in a new column
    df_meta['min_size'] = df_meta[['ImageWidth', 'ImageHeight']].min(axis=1)

    # Determine the maximum minimum size
    max_min_size = df_meta['min_size'].max()

    # Create a list of intervals based on the interval size and number of intervals
    inter = [i * interval_size for i in range(nb_intervals + 1)]

    # Create a list of labels for each interval
    labels = [f'{inter[i]}-{inter[i + 1]}' for i in range(nb_intervals)]

    # Categorize each image based on its size and interval
    df_meta['size_category'] = pd.cut(df_meta['min_size'], bins=inter, labels=labels)

    # Count the number of images in each category
    size_counts = df_meta['size_category'].value_counts()

    return display_bar(title='Number of images per size category', x_label='Size category', y_label='Number of images',
                       x_values=size_counts.index, y_values=size_counts.values)


@app.route('/graph/size/dynamic', methods=['POST'])
def graph_images_size_dynamic(nb_intervals=7, graph_type='all'):
    """
    Graph the number of images per size category
    The interval size is calculated dynamically

    :param nb_intervals: The number of intervals in the graph
    :param graph_type: The type of graph to display (bar, pie or all for both)
    """

    df_meta = get_metadata()

    # Calculate the minimum size of each image and store it in a new column
    df_meta['min_size'] = df_meta[['ImageHeight', 'ImageWidth']].min(axis=1)

    # Determine the maximum minimum size and calculate the number of bins dynamically based on the number of columns
    max_min_size = df_meta['min_size'].max()
    num_images = len(df_meta)
    num_bins = int(num_images / (num_images / nb_intervals))

    # Create a list of bins based on the maximum minimum size and number of bins
    bins = [i * (max_min_size / num_bins) for i in range(num_bins + 1)]

    # Create a list of labels for each bin
    labels = [f'{int(bins[i])}-{int(bins[i + 1])}' for i in range(num_bins)]

    # Categorize each image based on its size and bin
    df_meta['size_category'] = pd.cut(df_meta['min_size'], bins=bins, labels=labels)

    # Count the number of images in each category
    size_counts = df_meta['size_category'].value_counts()

    title = 'Number of images per size category'
    x_label = 'Image size'
    y_label = 'Number of images'

    # Create the appropriate chart based on the graph type parameter
    if graph_type == 'bar':
        return display_bar(title=title, x_label=x_label, y_label=y_label, x_values=size_counts.index,
                           y_values=size_counts.values)
    elif graph_type == 'pie':
        return display_pie(title=title, values=size_counts.values, labels=size_counts.index)
    elif graph_type == 'all':
        bar = display_bar(title=title, x_label=x_label, y_label=y_label, x_values=size_counts.index,
                          y_values=size_counts.values)
        pie = display_pie(title=title, values=size_counts.values, labels=size_counts.index)
        return bar, pie
    else:
        raise ValueError('Invalid graph type')


@app.route('/graph/datetime', methods=['POST'])
def graph_images_datetime(nb_intervals=10, graph_type='all'):
    """
    Graph the number of images per year

    :param graph_type: The type of graph to display (bar, pie, curve or all for all)
    :param nb_intervals: The number of intervals to display
    """
    df_meta = get_metadata()

    # Extract year from the 'DateTime' column and create a new 'Year' column
    df_meta['Year'] = pd.DatetimeIndex(df_meta['DateTimeOriginal']).year

    # Group the data by year and count the number of images for each year
    image_count = df_meta.groupby('Year').size().reset_index(name='count').sort_values('count', ascending=False)[
                  :nb_intervals]
    image_count['Year'] = image_count['Year'].astype(int)

    # Set the title of the graph
    title = 'Number of images per year'
    x_label = 'Year'
    y_label = 'Number of images'

    # Display different types of graphs based on the 'graph_type' parameter
    if graph_type == 'bar':
        # Display a bar chart
        image_count.plot(kind='bar', x='Year', y='count')
        return display_bar(title=title, x_label=x_label, y_label=y_label, x_values=image_count['Year'],
                           y_values=image_count['count'])

    elif graph_type == 'pie':
        # Display a pie chart using a custom function 'display_pie'
        return display_pie(title=title, values=image_count['count'], labels=image_count['Year'])

    elif graph_type == 'curve':
        # Display a line chart using a custom function 'display_curve'
        image_count = df_meta.groupby('Year').size().reset_index(name='count').sort_values('Year', ascending=True)
        return display_curve(title=title, x_label=x_label, y_label=y_label, x_values=image_count['Year'],
                             y_values=image_count['count'])

    elif graph_type == 'all':
        # Display all three types of graphs: bar, pie, and line charts

        # Bar chart
        image_count.plot(kind='bar', x='Year', y='count')
        bar = display_bar(title=title, x_label=x_label, y_label=y_label, x_values=image_count['Year'],
                          y_values=image_count['count'])

        # Pie chart
        pie = display_pie(title=title, values=image_count['count'], labels=image_count['Year'])

        # Line chart
        image_count = image_count.sort_values('Year', ascending=True)
        line = display_curve(title=title, x_label=x_label, y_label=y_label, x_values=image_count['Year'],
                             y_values=image_count['count'])

        return bar, pie, line
    else:
        # Raise an error if an invalid 'graph_type' parameter is passed
        raise ValueError('Invalid graph type')


@app.route('/graph/brand', methods=['POST'])
def graph_images_brand(graph_type='all', nb_columns=5):
    """
    Graph the number of images per brand

    :param graph_type: The type of graph to display (bar, pie or all for both)
    :param nb_columns: The number of columns to display
    """
    df_meta = get_metadata()

    # Initialize an empty dictionary to store the counts of each brand
    counts = {}

    # Loop through each brand in the metadata and count the number of occurrences
    for make in df_meta['Make']:
        if make is not None:
            counts[make] = counts.get(make, 0) + 1

    sorted_counts = dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    # Convert the dictionary into two lists of labels and values for graphing
    labels = list(sorted_counts.keys())[:nb_columns]
    values = list(sorted_counts.values())[:nb_columns]

    # Set the title for the graph
    title = 'Number of images per brand'
    x_label = 'Brand'
    y_label = 'Number of images'

    # Determine which type of graph to display based on the 'graph_type' parameter
    if graph_type == 'bar':
        # Display a bar graph
        return display_bar(title=title, x_label=x_label, y_label=y_label, x_values=labels, y_values=values)
    elif graph_type == 'pie':
        # Display a pie chart
        return display_pie(title=title, values=values, labels=labels)
    elif graph_type == 'all':
        # Display both a bar graph and a pie chart
        bar = display_bar(title=title, x_label=x_label, y_label=y_label, x_values=labels, y_values=values)
        pie = display_pie(title=title, values=values, labels=labels)
        return bar, pie
    else:
        # Raise an error if the 'graph_type' parameter is invalid
        raise ValueError('Invalid graph type')


def get_coordinates(df_meta):
    """
    Extract the coordinates of the images with GPS data

    :param df_meta: The metadata to extract the coordinates from
    """
    coords = {}
    for file, lattitude, longitude, altitude in zip(
            df_meta['filename'],
            df_meta['Latitude'],
            df_meta['Longitude'],
            df_meta['Altitude']
    ):
        if lattitude is not None and not np.isnan(lattitude) \
                and longitude is not None and not np.isnan(longitude):
            coords.update({file: [lattitude, longitude, altitude]})

    return get_country(coords)


def get_country(coordinates):
    """
    Get the country of each coordinate

    :param coordinates: The coordinates to get the country from
    :return: The coordinates with the country added
    """
    # Create a geolocator
    geolocator = Nominatim(user_agent="geoapiExercises")
    coordinates_list = coordinates.copy()

    # Get the continent information for each coordinate
    for key, coord in tqdm(coordinates_list.items(), desc='Getting country information'):
        if len(coord) < 4:  # If the country hasn't been found yet
            try:
                location = geolocator.reverse(coord, exactly_one=True, language='en')
                address = location.raw['address']
                country = address.get('country')
                coordinates[key].append(country)
            except:
                print(f"Error with {key} : {coord}")

    return coordinates


@app.route('graph/gps/map', methods=['POST'])
def display_coordinates_on_map(output_type='html'):
    """
    Display the coordinates on a map

    :param output_type: The output type (either 'html' or 'png')
    :return: The map with the coordinates displayed as markers
    """

    df_meta = get_metadata()

    coordinates_list = get_coordinates(df_meta)

    # create a map centered at a specific location
    m = folium.Map(location=[0, 0], zoom_start=1)

    # add markers for each set of coordinates
    for image, coords in coordinates_list.items():
        lat, lon, alt = coords
        folium.Marker(location=[lat, lon], tooltip=image, popup=f'file:{image}\ncoord:{coords}').add_to(m)

    # Export the map to the desired output type
    if output_type == 'html':
        # Save the map to an HTML file
        html_map = m._repr_html_()
        with open('map.html', 'w') as f:
            f.write(html_map)

        # Read the HTML file contents
        with open('map.html', 'rb') as f:
            html_bytes = f.read()

        # Return the HTML file contents as a response
        return Response(html_bytes, mimetype='text/html')
    elif output_type == 'png':
        # Save the map to an in-memory buffer
        buffer = io.BytesIO()
        m.save(buffer)

        # Return the buffer contents as a response
        return Response(buffer.getvalue(), mimetype='image/png')
    else:
        raise ValueError("Invalid output_type. Must be 'html' or 'png'.")


@app.route('graph/gps/continent', methods=['POST'])
def graph_images_countries(nb_inter=5, graph='all'):
    """
    Display graphs about the number of images by country

    :param nb_inter: number of interval
    :param graph: type of graph to display (bar, pie, all)
    """
    df_meta = get_metadata()
    coord_list = get_coordinates(df_meta)

    # Create a pandas DataFrame from the coordinates dictionary
    df = pd.DataFrame.from_dict(coord_list, orient='index',
                                columns=['Latitude', 'Longitude', 'Altitude', 'Country'])

    # Group the DataFrame by continent and count the number of images
    country_count = df.groupby('Country')['Country'].count()
    country_count = country_count.sort_values(ascending=False)[:nb_inter]

    title = 'Number of images by country'
    x_label = 'Country'
    y_label = 'Image Count'

    if graph == 'bar':
        return display_bar(title=title, x_label=x_label, y_label=y_label, x_values=country_count.index,
                           y_values=country_count.values)
    elif graph == 'pie':
        return display_pie(title=title, values=country_count.values, labels=country_count.index)
    else:
        bar = display_bar(title=title, x_label=x_label, y_label=y_label, x_values=country_count.index,
                          y_values=country_count.values)
        pie = display_pie(title=title, values=country_count.values, labels=country_count.index)
        return bar, pie


@app.route('graph/gps/altitude', methods=['POST'])
def graph_images_altitudes(coord_list, nb_inter=5, graph='all'):
    """
    Display graphs about the number of images by altitude.

    :param coord_list: list of coordinates
    :param nb_inter: number of interval
    :param graph: type of graph to display (histogram, pie, all)
    """

    altitudes = []
    for img in coord_list:
        alt = float(coord_list[img][2])
        if alt > 0.0:
            altitudes.append(alt)

    # Créer les intervalles en utilisant linspace() de numpy
    intervalles = np.linspace(0, max(altitudes), nb_inter + 1)

    # Convertir les intervalles en paires d'intervalles
    intervalles = [(int(intervalles[i]), int(intervalles[i + 1])) for i in range(len(intervalles) - 1)]

    # Compte combien d'altitudes se situent dans chaque intervalle
    counts = [0] * len(intervalles)
    for altitude in altitudes:
        for i, intervalle in enumerate(intervalles):
            if intervalle[0] <= altitude < intervalle[1]:
                counts[i] += 1

    # Créer une liste de noms pour les intervalles
    noms_intervalles = ["{}-{}".format(intervalle[0], intervalle[1]) for intervalle in intervalles]

    title = 'Number of images by altitude'
    x_label = 'Altitude'
    y_label = 'Image Count'

    if graph == 'histogram':
        return display_histogram(title=title, x_label=x_label, y_label=y_label, x_values=altitudes, bins=nb_inter)
    elif graph == 'pie':
        return display_pie(title=title, values=counts, labels=noms_intervalles)
    elif graph == 'bar':
        return display_bar(title=title, x_label=x_label, y_label=y_label, x_values=noms_intervalles, y_values=counts)
    else:
        histo = display_histogram(title=title, x_label=x_label, y_label=y_label, x_values=altitudes, bins=nb_inter)
        bar = display_bar(title=title, x_label=x_label, y_label=y_label, x_values=noms_intervalles, y_values=counts)
        pie = display_pie(title=title, values=counts, labels=noms_intervalles)
        return histo, bar, pie


def closest_colour(requested_colour):
    """
    Find the closest color in the webcolors library

    :param requested_colour: color to find
    :return: the closest color
    """
    min_colours = {}
    for key, name in webcolors.CSS3_HEX_TO_NAMES.items():
        r_c, g_c, b_c = webcolors.hex_to_rgb(key)
        rd = (r_c - requested_colour[0]) ** 2
        gd = (g_c - requested_colour[1]) ** 2
        bd = (b_c - requested_colour[2]) ** 2
        min_colours[(rd + gd + bd)] = name
    return min_colours[min(min_colours.keys())]


def get_colour_name(requested_colour):
    """
    Get the name of the closest color

    :param requested_colour: color to find
    :return: the actual name and the closest name
    """
    try:
        closest_name = actual_name = webcolors.rgb_to_name(requested_colour)
    except ValueError:
        closest_name = closest_colour(requested_colour)
        actual_name = None
    return actual_name, closest_name


@app.route('graph/dominant_color', methods=['POST'])
def graph_dominant_colors(nb_inter=5, graph='all'):
    """
    Display graphs about the number of images by dominant color

    :param nb_inter: number of interval
    :param graph: type of graph to display (bar, pie, treemap, all)
    """

    df_meta = get_metadata()

    # Create a dictionary of dominant colors
    dict_dom_color = {}
    df_dict_meta = df_meta["dominant_color"].to_dict()

    # convert string of dom color to list
    for img in df_dict_meta:
        try:
            if df_dict_meta[img] is not None and df_dict_meta[img] is not np.nan:
                list_dom_color = eval(df_dict_meta[img])
                dict_dom_color.update({img: list_dom_color})
        except:
            print(f"Error with {img} : {df_dict_meta[img]}")

    # Count the number of times each color appears
    color_counts = Counter()
    for image_colors in dict_dom_color.values():
        for color, percentage in image_colors:
            color_counts[color] += percentage

    # Map hexadecimal codes to color names
    color_names = {}
    for code in color_counts.keys():
        try:
            rgb = webcolors.hex_to_rgb(code)
            actual, closest = get_colour_name(rgb)
            color_names[code] = closest
        except ValueError:
            pass

    # Create a dictionary of color percentages
    dict_res = {}
    for key, val in color_names.items():
        if val in dict_res:
            dict_res[val] += round(color_counts[key] / 100, 5)
        else:
            dict_res[val] = round(color_counts[key] / 100, 5)

    # Create a bar graph showing the dominant colors in the images
    if sum(dict_res.values()) > 100:
        raise Exception('Error : sum of percentages is greater than 100')

    columns = dict_res.__len__()
    if columns > nb_inter: columns = nb_inter

    # Sort the dictionary by value
    sorted_colors = sorted(dict_res.items(), key=lambda x: x[1], reverse=True)
    top_colors = dict(sorted_colors[:columns])
    color_labels = list(top_colors.keys())
    sizes = list(top_colors.values())
    color = [webcolors.name_to_hex(c) for c in top_colors]

    title = 'Top Colors'
    x_label = 'Color'
    y_label = 'Percentage'

    if graph == 'bar':
        return display_bar(title=title, x_label=x_label, y_label=y_label, colors=top_colors.keys(),
                           x_values=top_colors.keys(), y_values=top_colors.values())
    elif graph == 'pie':
        return display_pie(title=title, values=top_colors.values(), labels=top_colors.keys(), colors=color_labels)
    elif graph == 'treemap':
        return display_tree_map(title=title, sizes=sizes, labels=color_labels, colors=color, alpha=.7)
    else:
        bar = display_bar(title=title, x_label=x_label, y_label=y_label, colors=top_colors.keys(),
                          x_values=top_colors.keys(), y_values=top_colors.values())
        pie = display_pie(title=title, values=top_colors.values(), labels=top_colors.keys(), colors=color_labels)
        treemap = display_tree_map(title=title, sizes=sizes, labels=color_labels, colors=color, alpha=.7)
        return bar, pie, treemap


@app.route('graph/tags/top', methods=['POST'])
def graph_top_tags(nb_inter=5, graph='all'):
    """
    Display graphs about the number of images by tags

    :param nb_inter: number of interval
    :param graph: type of graph to display (bar, pie, treemap, all)
    """

    df_meta = get_metadata()

    all_tags = []
    for tags in df_meta['tags']:
        if tags is not None and tags is not np.nan:
            all_tags += tags

    top_tags = dict(collections.Counter(all_tags).most_common(nb_inter))

    title = 'Top Tags'
    x_label = 'Tag'
    y_label = 'Count'

    if graph == 'bar':
        return display_bar(title=title, x_label=x_label, y_label=y_label,
                           x_values=top_tags.keys(), y_values=top_tags.values())
    elif graph == 'pie':
        return display_pie(title=title, values=top_tags.values(), labels=top_tags.keys())
    else:
        bar = display_bar(title=title, x_label=x_label, y_label=y_label,
                          x_values=top_tags.keys(), y_values=top_tags.values())
        pie = display_pie(title=title, values=top_tags.values(), labels=top_tags.keys())
        return bar, pie


def categorize_tags(df_meta, categories_list: list):
    """
    Categorize tags based on similarity to category prototypes

    :param categories_list: list of categories
    :param df_meta: DataFrame of metadata
    :return: dictionary of categories
    """
    # Concatène toutes les listes de tags
    all_tags = []
    for tags in df_meta['tags']:
        if tags is not None and tags is not np.nan:
            all_tags += tags

    # Load pre-trained word embedding model
    nlp = spacy.load("en_core_web_lg")

    categories = {}
    for cate in categories_list:
        categories[cate] = {}

    # categorize words based on similarity to category prototypes
    for word in tqdm(all_tags, desc="Categorizing tags"):
        # find the most similar category prototype for the word
        max_similarity = -1
        chosen_category = "other"
        for category in categories:
            similarity = nlp(word).similarity(nlp(category))
            if similarity > max_similarity:
                max_similarity = similarity
                chosen_category = category

        # add the word into the appropriate category dictionary
        categories[chosen_category].update({word: max_similarity})

    return categories


@app.route('graph/tags/dendrogram', methods=['POST'])
def graph_categorized_tags(categories_list: list):
    """
    Display a Denrogram of categorized tags

    :param categories_list: list of categories
    """
    df_meta = get_metadata()

    categorized_tags = categorize_tags(df_meta, categories_list)

    keys_and_subkeys = []
    for key, subdict in categorized_tags.items():
        for subkey in subdict:
            keys_and_subkeys.append((key, subkey))

    labels = [f"{key} -> {subkey}" for key, subkey in keys_and_subkeys]

    def custom_distance(x, y):
        key1, subkey1 = x
        key2, subkey2 = y
        if key1 == key2:
            return abs(categorized_tags[key1][subkey1] - categorized_tags[key2][subkey2])
        else:
            return 1.0

    dist_matrix = pdist(keys_and_subkeys, custom_distance)
    Z = linkage(dist_matrix, method='average')

    fig = plt.figure(figsize=(10, 7))
    dendrogram(Z, labels=labels, orientation='top', leaf_font_size=10)
    plt.xlabel("Distance")
    plt.tight_layout()

    # Save the plot to a buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    buffer.seek(0)

    # Return the buffer contents as a response
    return Response(buffer.getvalue(), mimetype='image/png')
